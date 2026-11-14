; Synthetic AIR kernel for tests/test_iridium_lifetime_markers.py. The internal helper indexes a
; local array with a value only known at run time, so SROA cannot promote the alloca and the
; llvm.lifetime markers around it survive always-inline into the kernel body.
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@index = internal global i32 1
@result = internal global i32 0

define internal i32 @pick(i32 %i) {
entry:
  %table = alloca [4 x i32], align 4
  call void @llvm.lifetime.start.p0(i64 16, ptr %table)
  store i32 10, ptr %table, align 4
  %t1 = getelementptr inbounds [4 x i32], ptr %table, i64 0, i64 1
  store i32 11, ptr %t1, align 4
  %t2 = getelementptr inbounds [4 x i32], ptr %table, i64 0, i64 2
  store i32 12, ptr %t2, align 4
  %t3 = getelementptr inbounds [4 x i32], ptr %table, i64 0, i64 3
  store i32 13, ptr %t3, align 4
  %wide = zext i32 %i to i64
  %slot = getelementptr inbounds [4 x i32], ptr %table, i64 0, i64 %wide
  %value = load i32, ptr %slot, align 4
  call void @llvm.lifetime.end.p0(i64 16, ptr %table)
  ret i32 %value
}

define void @probe() {
entry:
  %i = load i32, ptr @index, align 4
  %value = call i32 @pick(i32 %i)
  store i32 %value, ptr @result, align 4
  ret void
}

declare void @llvm.lifetime.start.p0(i64 immarg, ptr nocapture)
declare void @llvm.lifetime.end.p0(i64 immarg, ptr nocapture)

!air.kernel = !{!0}
!0 = !{ptr @probe, !1, !1}
!1 = !{}
