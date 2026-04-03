# ClaudeSpend
A local-first dashboard to track and visualize Anthropic API usage, cost, and efficiency.

# claude-spend

See exactly where your Anthropic API credits go.

![Dashboard Screenshot](screenshot.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is this?

`claude-spend` is a local-first dashboard for tracking and visualizing Anthropic API spend in real time.

It connects to Anthropic's Admin APIs and shows:

- Total spend
- Token usage
- Burn rate and monthly projection
- Spend by model
- Daily spend trends
- Workspace breakdown
- Claude Code developer analytics
- Cache efficiency and estimated savings
- CSV and JSON export

It is self-hosted, lightweight, and runs locally with no build step required for the frontend.

## Features

- FastAPI backend
- Single-file HTML dashboard with vanilla JavaScript and Chart.js
- SQLite caching layer to reduce repeated API calls
- Local `.env` configuration for Admin API key
- CLI launcher with browser auto-open
- CSV export
- JSON export
- Claude Code analytics support
- Cache efficiency analysis
- Dark-mode dashboard

## Quick Start

### 1) Install

```bash
pip install claude-spend
