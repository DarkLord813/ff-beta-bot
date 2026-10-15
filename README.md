---
title: FF Beta Auto-Unlock Bot
emoji: 🤖
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# 🤖 FF Beta Auto-Unlock Bot

A Telegram bot that automatically verifies and unlocks Free Fire Beta accounts on `unlockffbeta.com`, with live progress updates and automatic re-unlocking when the website's timer expires.

## ✨ Features

- 🎯 **Live Progress Bar** — Users see real-time progress (0% → 100%) as the bot works
- 🔄 **Auto Re-Unlock** — Reads the website's exact timer (e.g., "56m 49s") and re-runs when it expires
- 🌐 **Free Proxy Rotation** — Auto-fetches and validates free proxies on startup
- 📊 **Persistent User Database** — Remembers each user's account ID so they only enter it once
- 💓 **Keep-Alive Ping** — Prevents Hugging Face / Render free tiers from sleeping
- 🛡️ **Ad Gate Handling** — Automatically bypasses popups and ad overlays
- 🎨 **Beautiful Telegram Messages** — Markdown-formatted responses

## 📁 Project Structure
