// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// Album covers off the microSD, decoded with the ESP32-S3 ROM's TJpgDec:
// /agentpet/covers/<hash12>.jpg, baseline JPEG <= 200x200,
// pushed by the host. Zero new libraries — jd_prepare/jd_decomp live in mask
// ROM (esp32s3.rom.ld) and the decoder writes straight into one PSRAM
// RGB565 buffer that the play page blits with draw16bitRGBBitmap.
//
// One entry of cache: the play page asks for the same path 30x a second, so
// both the decoded image AND a failure (missing file, not a baseline JPEG)
// are remembered per path and never retried until the path changes.
#pragma once
#include <stdint.h>

static const int COVER_MAX = 200;      // biggest cover we keep (80 KB PSRAM)

struct CoverImg {
  const uint16_t* px;   // COVER_MAX-stride RGB565 rows, nullptr = nothing to draw
  int w, h;             // decoded size (<= COVER_MAX)
};

CoverImg coverGet(const char* path);   // "" or a bad path -> {nullptr,0,0}
void coverForget(const char* path);    // that file just landed/changed: decode it again
