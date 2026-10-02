// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// Card-backed CJK glyphs (SD 全量中文字库, 2026-09-03). The flash tables in
// almanac_font.h stay as they are; any glyph they lack is fetched from
// /agentpet/fonts/<face>.afn on the card (index in PSRAM, glyph records in a
// PSRAM cache). Adding words to the almanac no longer needs a re-bake.
#pragma once
#include <Arduino.h>

enum SdFace : uint8_t { SDF_HEAD = 0, SDF_BODY = 1, SDF_QUOT = 2, SDF_TINY = 3, SDF_N = 4 };

void sdFontLoadAll();                        // (re)open every face; safe to call again
bool sdFontReady(uint8_t face);
const uint8_t* sdFontGlyph(uint8_t face, uint32_t cp);   // cached record, nullptr if absent
bool sdFontIsFontPath(const char* path);     // pushed file lands under /agentpet/fonts/
