#!/usr/bin/env python3
"""Offline charts. No HTTP calls; unsupported chart types fail explicitly."""
import argparse
import json
import math
from pathlib import Path


def generate(spec, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    chart = spec['tool'].removeprefix('generate_').removesuffix('_chart')
    if chart not in {'bar', 'column', 'line', 'area', 'pie', 'scatter'}:
        raise ValueError(f'Unsupported local chart type: {chart}')
    args = spec.get('args', {})
    data = args.get('data', [])
    if not data:
        raise ValueError('data must not be empty')
    if args.get('group') or args.get('stack') or any('group' in row for row in data):
        raise ValueError('Grouped/stacked charts require a dedicated local implementation')
    values = [row['y'] if chart == 'scatter' else row['value'] for row in data]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
        raise ValueError('Values must be finite numbers')
    if chart == 'pie' and (any(v < 0 for v in values) or sum(values) <= 0):
        raise ValueError('Pie values must be nonnegative with a positive sum')
    output = Path(output)
    if output.suffix.lower() not in {'.svg', '.png', '.pdf'}:
        raise ValueError('Output must be SVG, PNG or PDF')
    labels = [str(row.get('category', row.get('time', i + 1))) for i, row in enumerate(data)]
    # Keep SVG text editable and renderable with the viewer's local fonts.
    plt.rcParams['svg.fonttype'] = 'none'
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'DejaVu Sans']
    fig, ax = plt.subplots(figsize=(10, 6), layout='constrained')
    try:
        if chart == 'bar':
            ax.barh(labels, values)
        elif chart == 'column':
            ax.bar(labels, values)
        elif chart in {'line', 'area'}:
            ax.plot(labels, values, marker='o')
            if chart == 'area':
                ax.fill_between(range(len(values)), values, alpha=.2)
        elif chart == 'pie':
            ax.pie(values, labels=labels, autopct='%1.1f%%')
        else:
            xs = [row['x'] for row in data]
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in xs):
                raise ValueError('x must contain finite numbers')
            ax.scatter(xs, values)
        ax.set_title(args.get('title', ''))
        ax.set_xlabel(args.get('axisXTitle', ''))
        ax.set_ylabel(args.get('axisYTitle', ''))
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output)
    finally:
        plt.close(fig)
    return str(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', help='JSON specification file')
    parser.add_argument('--output-file', required=True)
    args = parser.parse_args()
    try:
        print(generate(json.loads(Path(args.spec).read_text()), args.output_file))
    except (ImportError, ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f'Chart generation failed: {exc}\n')


if __name__ == '__main__':
    main()
