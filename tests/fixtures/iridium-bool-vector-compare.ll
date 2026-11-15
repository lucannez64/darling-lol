; Synthetic AIR kernel for tests/test_iridium_inlined_helpers.py: the helper compares two <4 x i1> values.
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@a = internal global <4 x i32> <i32 1, i32 2, i32 3, i32 4>
@b = internal global <4 x i32> <i32 4, i32 3, i32 2, i32 1>
@result = internal global <4 x i32> zeroinitializer

define internal <4 x i32> @same(<4 x i32> %x, <4 x i32> %y) noinline optnone {
entry:
  %low = icmp slt <4 x i32> %x, %y
  %big = icmp sgt <4 x i32> %x, <i32 2, i32 2, i32 2, i32 2>
  %odd = trunc <4 x i32> %y to <4 x i1>
  %equal = icmp eq <4 x i1> %low, %big
  %differs = icmp ne <4 x i1> %low, %odd
  %both = and <4 x i1> %equal, %odd
  %either = or <4 x i1> %both, %differs
  %flip = xor <4 x i1> %either, %big
  %mask = sext <4 x i1> %flip to <4 x i32>
  %ones = zext <4 x i1> %equal to <4 x i32>
  %sum = add <4 x i32> %mask, %ones
  ret <4 x i32> %sum
}

define void @probe() {
entry:
  %x = load <4 x i32>, ptr @a, align 16
  %y = load <4 x i32>, ptr @b, align 16
  %value = call <4 x i32> @same(<4 x i32> %x, <4 x i32> %y)
  store <4 x i32> %value, ptr @result, align 16
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @probe, !1, !1}
!1 = !{}
