; Synthetic AIR kernel for tests/test_iridium_inlined_helpers.py: the helper orders two i1 values, which Iridium does not lower.
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@a = internal global i32 1
@b = internal global i32 5
@result = internal global i32 0

define internal i32 @order(i32 %x, i32 %y) noinline optnone {
entry:
  %low = icmp slt i32 %x, %y
  %big = icmp sgt i32 %x, 3
  %below = icmp ult i1 %low, %big
  %value = zext i1 %below to i32
  ret i32 %value
}

define void @probe() {
entry:
  %x = load i32, ptr @a, align 4
  %y = load i32, ptr @b, align 4
  %value = call i32 @order(i32 %x, i32 %y)
  store i32 %value, ptr @result, align 4
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @probe, !1, !1}
!1 = !{}
