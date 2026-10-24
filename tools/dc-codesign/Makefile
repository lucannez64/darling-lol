CC ?= cc
CLANG ?= clang
CFLAGS ?= -D_GNU_SOURCE -O2 -g -Wall -Wextra -std=c11 -Wno-deprecated-declarations
LDLIBS ?= -lcrypto

BIN := dc-codesign
FUZZ_BIN := dc-parse-fuzz
SRC := main.c dc_codesign.c
FUZZ_FLAGS := -D_GNU_SOURCE -g -O1 -fsanitize=fuzzer,address,undefined -fno-omit-frame-pointer

.PHONY: all clean test fuzz fuzz-run

all: $(BIN)

$(BIN): $(SRC) dc_codesign.h
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDLIBS)

test: $(BIN)
	./test.sh

$(FUZZ_BIN): fuzz/dc_parse_fuzz.c dc_codesign.c dc_codesign.h
	$(CLANG) $(FUZZ_FLAGS) -o $@ fuzz/dc_parse_fuzz.c dc_codesign.c -lcrypto

fuzz: $(FUZZ_BIN)

fuzz-run: $(FUZZ_BIN)
	./fuzz/run.sh 60

clean:
	rm -f $(BIN) $(FUZZ_BIN)
