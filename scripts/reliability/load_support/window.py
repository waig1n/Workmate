"""One monotonic admission window; no restart aggregation or catch-up bursts."""
import time


class Window:
    def __init__(self,duration,rate,*,now=None,burst_rate=0):
        if not 0<duration<=259200 or not 0<rate<=100:
            raise ValueError('Invalid bounded window or arrival rate')
        self.start=time.monotonic() if now is None else now
        self.duration=duration;self.rate=rate;self.next=self.start;self.errors=[]
        if burst_rate and not rate<=burst_rate<=100:raise ValueError('Invalid burst rate')
        self.burst_rate=burst_rate

    def fail(self,reason):self.errors.append(reason)

    def admit(self,now):return not self.errors and now<self.start+self.duration

    def delay(self,now):return max(0,self.next-now)

    def sent(self,now):
        elapsed=now-self.start
        rate=self.burst_rate if self.burst_rate and elapsed>=21600 and elapsed%21600<600 else self.rate
        self.next=now+1/rate

    def result(self,now):
        return {'passed':not self.errors and now-self.start>=self.duration,
                'requested_seconds':self.duration,'elapsed_seconds':now-self.start,'errors':self.errors}
