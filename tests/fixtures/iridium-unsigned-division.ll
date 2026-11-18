; Exercise unsigned scalar division, vector remainder and logical right shift.
target datalayout = "e-p:64:64:64-i32:32:32-v64:64:64"
target triple = "air64-apple-macosx14.0.0"

@dividend = internal global <2 x i32> <i32 -1, i32 -2>
@divisor = internal global <2 x i32> <i32 7, i32 11>
@quotient = internal global i32 0
@shifted_quotient = internal global i32 0
@remainder = internal global <2 x i32> zeroinitializer

define internal void @divide(i32 %thread_position) alwaysinline {
entry:
  %lhs = load <2 x i32>, ptr @dividend, align 8
  %rhs = load <2 x i32>, ptr @divisor, align 8
  %denominator = extractelement <2 x i32> %rhs, i32 0
  %q = udiv i32 %thread_position, %denominator
  %shifted = lshr i32 %q, 1
  %r = urem <2 x i32> %lhs, %rhs
  store i32 %q, ptr @quotient, align 4
  store i32 %shifted, ptr @shifted_quotient, align 4
  store <2 x i32> %r, ptr @remainder, align 8
  ret void
}

define void @probe(i32 %thread_position) {
entry:
  call void @divide(i32 %thread_position)
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @probe, !1, !2}
!1 = !{}
!2 = !{!3}
!3 = !{i32 0, !"air.thread_position_in_grid"}
