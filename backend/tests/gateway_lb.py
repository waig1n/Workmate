"""Real local Nginx process for bounded Gateway acceptance, never a production proxy."""
import asyncio
import os
import re
from uuid import uuid4
from pathlib import Path
import shutil
import socket

import httpx


def nginx_config(directory, port, upstreams):
    ports = [port, *upstreams]
    if (len(upstreams) != 2 or len(set(ports)) != len(ports)
            or any(type(value) is not int or not 1 <= value <= 65535 for value in ports)):
        raise ValueError('Three distinct valid loopback ports are required')
    directory = Path(directory).resolve()
    if any(character in str(directory) for character in ('"', '\n', '\r', ';', '{', '}')):
        raise ValueError('Unsafe Nginx fixture directory')
    servers = '\n'.join(f'    server 127.0.0.1:{value} max_fails=1 fail_timeout=1s;' for value in upstreams)
    return f'''pid "{directory}/nginx.pid";
error_log "{directory}/nginx-error.log" info;
events {{ worker_connections 128; }}
http {{
  access_log "{directory}/nginx-access.log";
  client_body_temp_path "{directory}/client-temp";
  proxy_temp_path "{directory}/proxy-temp";
  fastcgi_temp_path "{directory}/fastcgi-temp";
  uwsgi_temp_path "{directory}/uwsgi-temp";
  scgi_temp_path "{directory}/scgi-temp";
  upstream gateways {{
{servers}
  }}
  server {{
    listen 127.0.0.1:{port};
    location / {{
      proxy_pass http://gateways;
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_read_timeout 15s;
      proxy_connect_timeout 1s;
      proxy_next_upstream error timeout;
      add_header X-Acceptance-Upstream $upstream_addr always;
    }}
  }}
}}
'''


def container_command(directory, image, name):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('An exact locally available image ID is required')
    if not re.fullmatch(r'workmate-lb-[a-f0-9]{32}', name):
        raise ValueError('An owned random container name is required')
    directory = Path(directory).resolve()
    if ',' in str(directory):
        raise ValueError('Comma in mount path is unsupported')
    owner = os.getenv('WORKMATE_TEST_ACCEPTANCE_OWNER')
    labels = []
    if owner:
        if not re.fullmatch(r'gateway-drill-[a-f0-9]{12}', owner):
            raise ValueError('Invalid outer acceptance owner')
        labels = ['--label', 'workmate.validation.owner=' + owner,
                  '--label', 'workmate.acceptance.role=proxy']
    return ['docker', 'run', '--rm', '--pull=never', '--name', name, *labels,
        '--label', f'workmate.acceptance.owner={name}', '--network=host', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--mount',
        f'type=bind,src={directory},dst={directory}', '--entrypoint', 'nginx', image,
        '-p', str(directory), '-c', str(directory / 'nginx.conf'), '-g', 'daemon off;']


async def docker_control(*arguments):
    process = await asyncio.create_subprocess_exec('docker', *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, error = await asyncio.wait_for(process.communicate(), 20)
    if process.returncode:
        raise AssertionError(f'Docker fixture operation failed: {error.decode()}')
    return output.decode().strip()


class Nginx:
    def __init__(self, directory, upstreams):
        self.directory = Path(directory)
        self.upstreams = upstreams
        self.process = self.output = None
        self.container = None

    async def start(self):
        executable = os.getenv('WORKMATE_TEST_NGINX') or shutil.which('nginx')
        image = os.getenv('WORKMATE_TEST_NGINX_IMAGE')
        if not executable and not image:
            raise RuntimeError('Set WORKMATE_TEST_NGINX or WORKMATE_TEST_NGINX_IMAGE')
        with socket.socket() as endpoint:
            endpoint.bind(('127.0.0.1', 0))
            port = endpoint.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        config = self.directory / 'nginx.conf'
        config.write_text(nginx_config(self.directory, port, self.upstreams))
        self.output = (self.directory / 'nginx-process.log').open('ab')
        if image:
            self.container = 'workmate-lb-' + uuid4().hex
            command = container_command(self.directory, image, self.container)
        else:
            command = [executable, '-p', str(self.directory), '-c', str(config), '-g', 'daemon off;']
        self.process = await asyncio.create_subprocess_exec(*command,
            stdout=self.output, stderr=self.output)
        async with httpx.AsyncClient(timeout=1) as client:
            async with asyncio.timeout(10):
                while True:
                    assert self.process.returncode is None, (self.directory / 'nginx-process.log').read_text()
                    try:
                        if (await client.get(self.url + '/health')).status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)

    async def close(self):
        if self.container:
            # Never stop or delete by broad project/name prefix; require our exact label.
            matches = await docker_control('ps', '-aq', '--filter', f'name=^/{self.container}$',
                '--filter', f'label=workmate.acceptance.owner={self.container}')
            if matches:
                await docker_control('stop', '--time', '5', self.container)
            remaining = await docker_control('ps', '-aq', '--filter', f'name=^/{self.container}$')
            assert not remaining, f'Owned Nginx container remains: {self.container}'
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                raise AssertionError('Nginx fixture required forced shutdown')
        if self.output:
            self.output.close()
