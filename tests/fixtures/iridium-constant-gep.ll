; Synthetic AIR kernel for tests/test_iridium_inlined_helpers.py: the helper reads a nested array global through a constant GEP that selects an inner array.
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@table = internal global [2 x [2 x float]] [[2 x float] [float 1.0, float 2.0], [2 x float] [float 3.0, float 4.0]]
@index = internal global i32 1
@result = internal global float 0.0

define internal float @pick(i32 %i) noinline optnone {
entry:
  %wide = zext i32 %i to i64
  %slot = getelementptr inbounds [2 x float], ptr getelementptr inbounds ([2 x [2 x float]], ptr @table, i64 0, i64 1), i64 0, i64 %wide
  %value = load float, ptr %slot, align 4
  ret float %value
}

define void @probe() {
entry:
  %i = load i32, ptr @index, align 4
  %value = call float @pick(i32 %i)
  store float %value, ptr @result, align 4
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @probe, !1, !1}
!1 = !{}
