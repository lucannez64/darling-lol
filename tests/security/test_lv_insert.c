#include <stdio.h>

__attribute__((constructor)) static void darling_inserted(void) {
	printf("INSERTED\n");
}
