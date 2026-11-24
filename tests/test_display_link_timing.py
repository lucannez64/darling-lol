"""Exercise shipped per-link scheduling with synthetic clocks and outputs."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime

PROVIDER = r'''
enum { DG_TIMING_NOMINAL=1, DG_TIMING_FEEDBACK=2, DG_TIMING_FIXED=4,
       DG_TIMING_COUNTER=8, DG_TIMING_SOFTWARE=16 };
struct DGTimingSnapshot {
 uint32_t version,flags,displayID,reserved;
 uint64_t processID,target,outputID,generation,rateNumerator,rateDenominator;
 uint64_t presentationNS,presentationCounter;
};
struct DGTimingCache { uint64_t epoch; DGTimingSnapshot snapshot; };
'''


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class DisplayLinkTimingChecks(unittest.TestCase):
    def test_mixed_rates_phase_changes_and_reentrant_lifecycle(self):
        source = runtime.CompatibilityChecks().patched(
            'darling', 'src/frameworks/CoreVideo/src/CVDisplayLink.mm')
        record = source[source.index('struct CVLinkStateRec {'):]
        record = record[:record.index('\n};') + 3]
        start = source.index('struct DisplayLinkClock {')
        bodies = source[start:source.index('/* Keep callbacks off', start)]
        for name in ('CVDisplayLinkSetCurrentCGDisplay',
                     'CVDisplayLinkSetCurrentCGDisplayFromOpenGLContext',
                     'CVDisplayLinkGetCurrentCGDisplay',
                     'CVDisplayLinkGetNominalOutputVideoRefreshPeriod',
                     'CVDisplayLinkGetCurrentTime', 'CVDisplayLinkStop'):
            bodies += runtime.function(source, name)
        bodies += runtime.function(source, 'CVDisplayLinkRelease').replace(
            '[(NSMutableDictionary*) displayLink release];', '++releases;')
        bodies += runtime.function(source, 'displayClockSleepNS')
        fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <climits>
#include <initializer_list>
using CVDisplayLinkRef=void*;
using CGLContextObj=void*;
using CGLPixelFormatObj=void*;
using CGDirectDisplayID=uint32_t;
using CVReturn=int;
using CVOptionFlags=uint64_t;
constexpr int kCVReturnSuccess=0,kCVReturnInvalidArgument=-1,kCVReturnError=-2;
constexpr uint32_t kCGNullDirectDisplay=0;
constexpr uint64_t kCVTimeStampHostTimeValid=1,kCVTimeStampVideoTimeValid=2;
constexpr uint64_t kCVTimeStampVideoRefreshPeriodValid=4,kCVTimeStampRateScalarValid=8;
constexpr int kCVTimeIsIndefinite=1;
struct CVTime { int64_t timeValue;int32_t timeScale,flags; };
struct CVTimeStamp {
 uint64_t hostTime=0;int64_t videoTime=0,videoRefreshPeriod=0;
 int32_t videoTimeScale=0;uint64_t flags=0;double rateScalar=0;
};
using CVDisplayLinkOutputCallback=CVReturn (*)(CVDisplayLinkRef,const CVTimeStamp*,
 const CVTimeStamp*,CVOptionFlags,CVOptionFlags*,void*);
struct mach_timebase_info { uint32_t numer,denom; } timebase;
static uint64_t hostTime;
static uint64_t mach_absolute_time() { return hostTime; }
static void mach_timebase_info(struct mach_timebase_info* out) { *out=timebase; }
static void (*traceLog)(const char*,...)=nullptr;
''' + PROVIDER + record + r'''
static CVLinkStateRec records[4];
static DGTimingSnapshot outputs[4];
static int releases,callbackMode;
static unsigned counts[4];
static uint64_t previousOutput[4];
static DGTimingSnapshot DGTimingRead(uint64_t context,uint32_t display,DGTimingCache* cache) {
 unsigned index=context?unsigned(context):display;if(index>3)index=0;
 cache->snapshot=outputs[index];return cache->snapshot;
}
static long clockRead(int,void* out,size_t size,long offset) {
 assert(offset>=0 && offset+size<=sizeof(records));
 memcpy(out,(char*)records+offset,size);return size;
}
static long slotOffset(uint32_t slot) { return (slot+1)*sizeof(CVLinkStateRec); }
static bool readHeader(int,CVLinkStateRec* out) { *out=records[0];return true; }
static bool readState(int,CVLinkStateRec* out,CVDisplayLinkRef link) {
 for(unsigned i=1;i<4;++i)if(records[i].alive && records[i].link==link) {
  *out=records[i];return true;
 }return false;
}
static bool writeState(int,const CVLinkStateRec* in) {
 for(unsigned i=1;i<4;++i)if(records[i].link==in->link){records[i]=*in;return true;}
 return false;
}
struct CVStateGuard { int fd=42; };
struct CVCallbackScope { bool ready=true; explicit CVCallbackScope(int) {} };
''' + bodies + r'''
static uint64_t ns() { return (__uint128_t(hostTime)*timebase.numer)/timebase.denom; }
static void setNS(uint64_t value) { hostTime=(__uint128_t(value)*timebase.denom+timebase.numer-1)/timebase.numer; }
static CVReturn callback(CVDisplayLinkRef link,const CVTimeStamp* now,
 const CVTimeStamp* output,CVOptionFlags,CVOptionFlags*,void*) {
 unsigned i=unsigned(uintptr_t(link));++counts[i];
 assert(now->videoTimeScale==1000000000 && output->videoTimeScale==1000000000);
 assert(now->videoTime==int64_t(ns()) && output->videoTime>now->videoTime);
 assert(output->videoTime>int64_t(previousOutput[i]));previousOutput[i]=output->videoTime;
 assert((__uint128_t(output->hostTime)*timebase.numer)/timebase.denom>=uint64_t(output->videoTime));
 CVTimeStamp queried;
 assert(CVDisplayLinkGetCurrentTime(link,&queried)==0 && queried.videoTime==now->videoTime);
 if(callbackMode==1)assert(CVDisplayLinkStop(link)==0);
 if(callbackMode==2)CVDisplayLinkRelease(link);
 return 0;
}
static void reset() {
 memset(records,0,sizeof records);memset(counts,0,sizeof counts);
 memset(previousOutput,0,sizeof previousOutput);callbackMode=0;records[0].refs=3;
 for(unsigned i=1;i<4;++i) {
  records[i].link=(void*)uintptr_t(i);records[i].callback=(void*)&callback;
  records[i].alive=records[i].running=records[i].refs=1;records[i].displayID=i;
 }records[3].running=0;
 for(unsigned i=0;i<4;++i) {
  outputs[i]={};outputs[i].version=1;outputs[i].flags=DG_TIMING_NOMINAL;
  outputs[i].displayID=i?i:1;outputs[i].target=i;outputs[i].generation=1;
  outputs[i].rateNumerator=i==2?144:60000;outputs[i].rateDenominator=i==2?1:1001;
 }
}
int main(int argc,char** argv) {
 assert(argc==3);timebase.numer=atoi(argv[1]);timebase.denom=atoi(argv[2]);reset();
 for(uint64_t t=0;t<=10000000000ull;t+=1000000){setNS(t);dispatchTimerFire(nullptr);}
 assert(counts[1]==600 && counts[2]==1441);
 assert(records[1].deadlineNS==uint64_t((__uint128_t(600)*1000000000ull*1001)/60000));
 assert(records[2].deadlineNS==uint64_t((__uint128_t(1441)*1000000000ull)/144));
 assert(displayClockSleepNS()<=16000000);
 CVTime period=CVDisplayLinkGetNominalOutputVideoRefreshPeriod((void*)1);
 assert(period.timeScale==1000000000 && period.timeValue==16683333 && !period.flags);
 unsigned before=counts[1];setNS(90000000000ull);dispatchTimerFire(nullptr);
 assert(counts[1]==before+1 && records[1].deadlineNS>ns());
 dispatchTimerFire(nullptr);assert(counts[1]==before+1);
 assert(CVDisplayLinkSetCurrentCGDisplayFromOpenGLContext((void*)1,(void*)2,nullptr)==0);
 assert(CVDisplayLinkGetCurrentCGDisplay((void*)1)==2);
 outputs[2].displayID=3;outputs[2].generation++;
 assert(CVDisplayLinkGetCurrentCGDisplay((void*)1)==3);
 assert(CVDisplayLinkSetCurrentCGDisplay((void*)1,1)==0 && !records[1].context);
 assert(CVDisplayLinkGetCurrentCGDisplay((void*)1)==1);
 outputs[1].rateNumerator=75;outputs[1].rateDenominator=1;outputs[1].generation++;
 setNS(ns()+1000000);dispatchTimerFire(nullptr);
 assert(CVDisplayLinkGetNominalOutputVideoRefreshPeriod((void*)1).timeValue==13333333);
 reset();setNS(1000000000);
 outputs[1].flags|=DG_TIMING_FEEDBACK|DG_TIMING_FIXED|DG_TIMING_COUNTER;
 outputs[1].rateNumerator=60;outputs[1].rateDenominator=1;
 outputs[1].presentationNS=980000000;outputs[1].presentationCounter=9;
 dispatchTimerFire(nullptr);assert(counts[1]==0 && records[1].deadlineNS==1013333333);
 setNS(1013333334);dispatchTimerFire(nullptr);assert(counts[1]==1);
 uint64_t next=records[1].deadlineNS;
 outputs[1].presentationNS=990000000;dispatchTimerFire(nullptr);
 assert(records[1].deadlineNS==next);
 // Feedback arriving on every due cycle cannot indefinitely defer callbacks.
 for(unsigned i=0;i<20;++i) {
  setNS(records[1].deadlineNS+100);
  outputs[1].presentationNS=ns()-100;outputs[1].presentationCounter++;
  before=counts[1];dispatchTimerFire(nullptr);assert(counts[1]==before+1);
 }
 for(uint32_t flags : {uint32_t(DG_TIMING_FEEDBACK),uint32_t(DG_TIMING_FEEDBACK|DG_TIMING_FIXED),
                      uint32_t(DG_TIMING_FEEDBACK|DG_TIMING_COUNTER)}) {
  reset();outputs[1].flags|=flags;outputs[1].presentationNS=999000000;
  outputs[1].presentationCounter=50;setNS(1000000000);dispatchTimerFire(nullptr);
  assert(counts[1]==1 && records[1].deadlineNS==1016683333);
 }
 reset();setNS(1000000000);
 outputs[1].flags|=DG_TIMING_FEEDBACK|DG_TIMING_FIXED|DG_TIMING_COUNTER;
 outputs[1].presentationNS=999000000;outputs[1].presentationCounter=0;
 dispatchTimerFire(nullptr);assert(counts[1]==1 && records[1].deadlineNS==1016683333);
 reset();setNS(10000000000ull);
 outputs[1].flags|=DG_TIMING_FEEDBACK|DG_TIMING_FIXED|DG_TIMING_COUNTER;
 outputs[1].presentationNS=1;outputs[1].presentationCounter=20;
 dispatchTimerFire(nullptr);assert(counts[1]==1);
 reset();setNS(0);callbackMode=1;dispatchTimerFire(nullptr);
 assert(!records[1].running && !records[2].running);
 setNS(100000000);dispatchTimerFire(nullptr);assert(counts[1]==1);
 reset();setNS(0);callbackMode=2;dispatchTimerFire(nullptr);
 assert(!records[1].alive && !records[2].alive && releases==2);
 reset();setNS(0);dispatchTimerFire(nullptr);
 assert(displayClockSleepNS()==6944444);
 records[2].running=0;assert(displayClockSleepNS()==16000000);
 CVTimeStamp stamp;
 assert(CVDisplayLinkGetCurrentTime(nullptr,&stamp)==kCVReturnInvalidArgument);
 assert(CVDisplayLinkGetCurrentTime((void*)1,nullptr)==kCVReturnInvalidArgument);
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'clock.cpp').write_text(fixture)
            result = subprocess.run(['c++', '-std=c++17', str(path / 'clock.cpp'),
                                     '-o', str(path / 'clock')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in (None, '30', '59.94', '144', 'invalid'):
                for numer, denom in ((1, 1), (3, 2), (125, 3)):
                    with self.subTest(inherited_refresh=value, timebase=(numer, denom)):
                        env = dict(os.environ)
                        env.pop('RCLOL_REFRESH_HZ', None)
                        if value is not None:
                            env['RCLOL_REFRESH_HZ'] = value
                        subprocess.run([str(path / 'clock'), str(numer), str(denom)],
                                       env=env, check=True, timeout=10)
