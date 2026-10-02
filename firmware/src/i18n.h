// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// 语言两档 (2026-09-24): 中文 = the board exactly as it was, English
// = every board-drawn Chinese string swapped for its English line. Runtime
// switch: host cfg {"lang":"zh"|"en"} -> NVS petgrow/lang -> g_lang; every
// page re-reads tr() each frame, so a change shows on the next redraw.
// NOT translated on purpose: the almanac page (kept in Chinese as an easter
// egg in English mode), 「命运自有回应」, 「黄历同步中」, 签二…签五.
#pragma once
#include <stdint.h>

enum Lang : uint8_t { LANG_ZH = 0, LANG_EN = 1, N_LANGS };
extern uint8_t g_lang;                 // defined in pages.cpp; 0 = zh (default)
inline bool langEn() { return g_lang == LANG_EN; }

enum StrId : uint8_t {
  S_APPROVE, S_HOLD_REJECT, S_SEND,          // face bubbles
  S_SKIN, S_LIGHT,                           // settings card
  S_PINNED, S_UNPINNED,                      // 钉住 名牌
  S_NP_LABEL, S_NP_LABEL_APP,                // play card labels (format strings)
  S_POD_LABEL_N, S_POD_LABEL,
  S_NP_EMPTY, S_NP_HINT, S_POD_EMPTY, S_POD_HINT,
  S_APP_NETEASE, S_APP_MUSIC, S_APP_PODCASTS, S_APP_CHROME, S_APP_SAFARI,
  S_CLAIM_Q, S_CLAIM_OK, S_CLAIM_DONE, S_CLAIM_NEW, S_CLAIM_FROM,   // claim card
  N_STRS
};

static const char* const STR[N_LANGS][N_STRS] = {
  {   // zh — byte-identical to the literals these replaced
    "批准", "长按=拒绝", "发送",
    "皮肤", "亮度",
    "钉住", "解除钉住",
    "正在播放 · Mac", "正在播放 · Mac · %s",
    "播客 · 第 %d 集 · 共 %d 集", "播客",
    "Mac 上没有在放的", "网易云、Apple Music、浏览器里放点什么就会出现在这里",
    "卡上还没有节目", "在 Mac 设置页「听什么」贴一个 RSS 或文件",
    "网易云音乐", "Apple Music", "播客", "Chrome", "Safari",
    "连到这台 Mac？", "连接", "已连接", "还没有主人", "现在跟着「%s」",
  },
  {   // en
    "Approve", "hold to reject", "Send",
    "Skin", "Light",
    "Pinned", "Unpinned",
    "Now Playing · Mac", "Now Playing · Mac · %s",
    "Podcast · Ep %d of %d", "Podcast",
    "Nothing playing on Mac", "Play something in NetEase, Apple Music or a browser",
    "No episodes on the card", "Add an RSS or file under Listen on the Mac settings page",
    "NetEase Music", "Apple Music", "Podcasts", "Chrome", "Safari",
    "Connect to this Mac?", "Connect", "Connected", "No owner yet", "Currently following \"%s\"",
  },
};

inline const char* tr(StrId id) {
  return STR[g_lang < N_LANGS ? g_lang : 0][id < N_STRS ? id : 0];
}

// English clock page: weekday header in full caps and a
// two-line classical time-of-day panel, twelve two-hour slots from 子时.
static const char* const EN_WEEKDAY[7] = {
    "SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"};
static const char* const EN_SHICHEN[12] = {
    "MIDNIGHT", "COCKCROW", "DAWN", "SUNRISE", "BREAKFAST", "FORENOON",
    "NOON", "AFTERNOON", "TEATIME", "SUNSET", "DUSK", "BEDTIME"};
static const char* const EN_SHICHEN_ANIMAL[12] = {
    "HOUR OF THE RAT", "HOUR OF THE OX", "HOUR OF THE TIGER",
    "HOUR OF THE RABBIT", "HOUR OF THE DRAGON", "HOUR OF THE SNAKE",
    "HOUR OF THE HORSE", "HOUR OF THE GOAT", "HOUR OF THE MONKEY",
    "HOUR OF THE ROOSTER", "HOUR OF THE DOG", "HOUR OF THE PIG"};
