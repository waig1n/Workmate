import { expect,it } from 'vitest';
import { zonedInstant } from './schedules';
it('converts wall time using selected zone, including fractional offsets',()=>{
 expect(zonedInstant('2027-01-01','09:00','Asia/Shanghai')).toBe('2027-01-01T01:00:00.000Z');
 expect(zonedInstant('2027-01-01','09:00','Asia/Kathmandu')).toBe('2027-01-01T03:15:00.000Z');
});
it('rejects missing DST times and chooses the first repeated instant',()=>{
 expect(()=>zonedInstant('2027-03-14','02:30','America/New_York')).toThrow();
 expect(zonedInstant('2027-11-07','01:30','America/New_York')).toBe('2027-11-07T05:30:00.000Z');
 expect(()=>zonedInstant('2027-01-01','09:00','wrong')).toThrow();
 expect(()=>zonedInstant('','09:00','UTC')).toThrow();
});
it('rejects malformed wall-clock values',()=>{
 expect(()=>zonedInstant('2027-13-99','09:00','UTC')).toThrow();
 expect(()=>zonedInstant('2027-01-01','25:00','UTC')).toThrow();
});
