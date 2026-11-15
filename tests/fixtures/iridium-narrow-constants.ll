; Synthetic AIR kernel for tests/test_iridium_inlined_helpers.py: the helper reads i8 and i16 array globals and uses i8 and i16 constants.
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@bytes = internal global [2 x i8] [i8 1, i8 -2]
@shorts = internal global [2 x i16] [i16 300, i16 -4]
@index = internal global i32 1
@result = internal global i32 0

define internal i32 @pick(i32 %i) noinline optnone {
entry:
  %wide = zext i32 %i to i64
  %byteSlot = getelementptr inbounds [2 x i8], ptr @bytes, i64 0, i64 %wide
  %byte = load i8, ptr %byteSlot, align 1
  %shortSlot = getelementptr inbounds [2 x i16], ptr @shorts, i64 0, i64 %wide
  %short = load i16, ptr %shortSlot, align 2
  %byteSum = add i8 %byte, 3
  %shortSum = add i16 %short, 500
  %first = sext i8 %byteSum to i32
  %second = sext i16 %shortSum to i32
  %sum = add i32 %first, %second
  ret i32 %sum
}

define void @probe() {
entry:
  %i = load i32, ptr @index, align 4
  %value = call i32 @pick(i32 %i)
  store i32 %value, ptr @result, align 4
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @probe, !1, !1}
!1 = !{}
