# Job Site Cookie Exporter Chrome Extension

One-click export of bossjob.us or linkedin.com cookies to your job scraper dashboard.

## Installation

1. **Open Chrome Extensions page:**
   - Go to `chrome://extensions/`
   - Enable **"Developer mode"** (toggle in top-right)

2. **Load the extension:**
   - Click **"Load unpacked"**
   - Select the `cookie-extension` folder (`e:\agentic-job-scraper\cookie-extension`)

3. **Pin the extension:**
   - Click the puzzle icon in Chrome toolbar
   - Pin "Job Site Cookie Exporter" for easy access

## Usage

1. **Login to bossjob.us or linkedin.com** in Chrome
2. **Click the extension icon** in Chrome toolbar
3. **Enter your settings:**
   - Dashboard API URL: `http://localhost:8000` (or your backend URL)
   - Select Website Source: Choose a bossjob or linkedin source from dropdown
4. **Click "Export Cookies to Dashboard"**

## How it works

- Extension reads cookies from the selected domain (`bossjob.us` or `linkedin.com`)
- Formats them for Playwright authentication
- Sends via API to update your website source
- Cookies are then used automatically when scraping

## Troubleshooting

- **"No cookies found"** - Make sure you're logged into the selected website
- **"Connection refused"** - Check that your backend is running
- **"No supported sources found"** - Add bossjob/linkedin sources in Websites page first

## Security

- Extension only reads cookies from bossjob.us and linkedin.com
- Cookies are sent directly to your backend API
- No data is stored or sent to third parties
