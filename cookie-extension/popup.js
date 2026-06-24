document.addEventListener('DOMContentLoaded', async () => {
  const exportBtn = document.getElementById('exportBtn');
  const statusDiv = document.getElementById('status');
  const apiUrlInput = document.getElementById('apiUrl');
  const sourceSelect = document.getElementById('sourceSelect');
  const refreshBtn = document.getElementById('refreshBtn');
  const cookiePreview = document.getElementById('cookiePreview');

  let supportedSources = [];

  const SITE_CONFIG = {
    bossjob: {
      domain: 'bossjob.us',
      aliases: ['bossjob.us', '.bossjob.us'],
      urls: ['https://bossjob.us/', 'https://www.bossjob.us/'],
      essentialCookies: ['sessionid', 'user', 'csrftoken', 'uuid'],
      requiredCookies: [],
      displayName: 'bossjob.us'
    },
    linkedin: {
      domain: 'linkedin.com',
      aliases: ['linkedin.com', '.linkedin.com', 'www.linkedin.com', '.www.linkedin.com'],
      urls: ['https://www.linkedin.com/', 'https://linkedin.com/'],
      essentialCookies: ['li_at', 'JSESSIONID', 'bcookie', 'liap'],
      requiredCookies: ['li_at'],
      displayName: 'linkedin.com'
    }
  };

  function getActiveTab() {
    return new Promise((resolve) => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        resolve(tabs[0] || null);
      });
    });
  }

  function getAllCookieStores() {
    return new Promise((resolve) => {
      if (!chrome.cookies || !chrome.cookies.getAllCookieStores) {
        resolve([]);
        return;
      }
      chrome.cookies.getAllCookieStores((stores) => resolve(stores || []));
    });
  }

  async function getStoreIdsForActiveTab(activeTabId) {
    const stores = await getAllCookieStores();
    if (!stores.length || typeof activeTabId !== 'number') return [];
    return stores
      .filter((s) => Array.isArray(s.tabIds) && s.tabIds.includes(activeTabId))
      .map((s) => s.id)
      .filter(Boolean);
  }

  function getCookies(details) {
    return new Promise((resolve) => {
      chrome.cookies.getAll(details, (cookies) => resolve(cookies || []));
    });
  }

  function getCookiesViaDebugger(tabId, urls) {
    return new Promise((resolve) => {
      if (typeof tabId !== 'number' || !chrome.debugger) {
        resolve([]);
        return;
      }

      const target = { tabId };

      const detachAndResolve = (cookies) => {
        chrome.debugger.detach(target, () => resolve(cookies || []));
      };

      chrome.debugger.attach(target, '1.3', () => {
        if (chrome.runtime.lastError) {
          resolve([]);
          return;
        }

        chrome.debugger.sendCommand(target, 'Network.enable', {}, () => {
          chrome.debugger.sendCommand(target, 'Network.getCookies', { urls }, (result) => {
            if (chrome.runtime.lastError) {
              detachAndResolve([]);
              return;
            }

            const cookies = (result?.cookies || []).map((c) => ({
              name: c.name,
              value: c.value,
              domain: c.domain,
              path: c.path || '/',
              secure: !!c.secure,
              httpOnly: !!c.httpOnly,
              sameSite: (c.sameSite || 'Lax').toLowerCase(),
              expirationDate: typeof c.expires === 'number' ? c.expires : undefined,
            }));

            detachAndResolve(cookies);
          });
        });
      });
    });
  }

  async function getCookiesForSite(siteConfig) {
    const allCookies = [];
    const dedup = new Set();

    const activeTab = await getActiveTab();
    const activeTabUrl = activeTab?.url || '';
    const activeTabId = activeTab?.id;
    const storeIds = await getStoreIdsForActiveTab(activeTabId);

    const urlsToCheck = [...(siteConfig.urls || [])];
    if (activeTabUrl && !urlsToCheck.includes(activeTabUrl)) {
      urlsToCheck.unshift(activeTabUrl);
    }

    async function collect(cookies) {
      for (const c of cookies) {
        const key = `${c.domain}|${c.path}|${c.name}`;
        if (dedup.has(key)) continue;
        dedup.add(key);
        allCookies.push(c);
      }
    }

    // URL-based query is more reliable for some Chromium cookie stores.
    for (const url of urlsToCheck) {
      await collect(await getCookies({ url }));
      for (const storeId of storeIds) {
        await collect(await getCookies({ url, storeId }));
      }
    }

    // Fallback: domain-based query.
    for (const domain of siteConfig.aliases) {
      await collect(await getCookies({ domain }));
      for (const storeId of storeIds) {
        await collect(await getCookies({ domain, storeId }));
      }
    }

    // Last resort: fetch all visible cookies and filter by domain suffix.
    if (allCookies.length === 0) {
      const allVisible = await getCookies({});
      const filtered = allVisible.filter((c) =>
        typeof c.domain === 'string' && c.domain.toLowerCase().includes(siteConfig.domain)
      );
      await collect(filtered);
    }

    // Final fallback for stubborn cases: use debugger protocol on active tab.
    if (allCookies.length === 0 && typeof activeTabId === 'number' && activeTabUrl.includes(siteConfig.domain)) {
      const debuggerCookies = await getCookiesViaDebugger(activeTabId, urlsToCheck);
      await collect(debuggerCookies);
    }

    return allCookies;
  }

  // Load saved API URL
  chrome.storage.local.get(['apiUrl'], (result) => {
    if (result.apiUrl) apiUrlInput.value = result.apiUrl;
    loadSources();
  });

  // Save API URL on change
  apiUrlInput.addEventListener('change', () => {
    chrome.storage.local.set({ apiUrl: apiUrlInput.value });
    loadSources();
  });

  // Refresh sources button
  refreshBtn.addEventListener('click', () => {
    loadSources();
  });

  function getSiteTypeFromSource(source) {
    const siteType = (source.site_type || '').toLowerCase();
    const url = (source.url || '').toLowerCase();

    if (siteType === 'bossjob' || url.includes('bossjob.us')) return 'bossjob';
    if (siteType === 'linkedin' || url.includes('linkedin.com')) return 'linkedin';
    return null;
  }

  // Load supported sources from API
  async function loadSources() {
    const apiUrl = apiUrlInput.value.trim();
    if (!apiUrl) {
      sourceSelect.innerHTML = '<option value="">Enter API URL first</option>';
      sourceSelect.disabled = true;
      return;
    }

    sourceSelect.innerHTML = '<option value="">Loading sources...</option>';
    sourceSelect.disabled = true;

    try {
      const response = await fetch(`${apiUrl}/api/website-sources`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      
      const data = await response.json();
      const sources = data.sources || [];
      
      // Filter for supported sources (bossjob/linkedin)
      supportedSources = sources
        .map((s) => ({ ...s, _siteType: getSiteTypeFromSource(s) }))
        .filter((s) => !!s._siteType);

      if (supportedSources.length === 0) {
        sourceSelect.innerHTML = '<option value="">No supported sources found</option>';
        showStatus('No bossjob/linkedin sources in dashboard. Add one first.', 'error');
        return;
      }

      // Populate dropdown
      sourceSelect.innerHTML = supportedSources.map(s =>
        `<option value="${s.id}">[${s._siteType}] ${s.name} (${s.url})</option>`
      ).join('');
      sourceSelect.disabled = false;
      showStatus(`Found ${supportedSources.length} supported source(s). Select one to export cookies.`, 'info');
    } catch (error) {
      sourceSelect.innerHTML = '<option value="">Error loading sources</option>';
      showStatus(`Error loading sources: ${error.message}`, 'error');
    }
  }

  exportBtn.addEventListener('click', async () => {
    const apiUrl = apiUrlInput.value.trim();
    const sourceId = sourceSelect.value;

    if (!apiUrl) {
      showStatus('Please enter API URL', 'error');
      return;
    }
    if (!sourceId) {
      showStatus('Please select a website source', 'error');
      return;
    }

    const selectedSource = supportedSources.find((s) => String(s.id) === String(sourceId));
    if (!selectedSource || !selectedSource._siteType) {
      showStatus('Selected source is not supported', 'error');
      return;
    }

    const siteType = selectedSource._siteType;
    const siteConfig = SITE_CONFIG[siteType];

    exportBtn.disabled = true;
    showStatus(`Fetching cookies from ${siteConfig.displayName}...`, 'info');

    try {
      // Get cookies from selected site (including aliases/subdomains)
      const cookies = await getCookiesForSite(siteConfig);
      
      if (cookies.length === 0) {
        const currentTabUrl = await new Promise((resolve) => {
          chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            resolve(tabs[0]?.url || '');
          });
        });
        showStatus(
          `No cookies found for ${siteConfig.displayName}. Open ${siteConfig.displayName} in current profile and refresh this popup. Current tab: ${currentTabUrl || '(unknown)'}`,
          'error'
        );
        cookiePreview.textContent =
          `Debug: no cookies returned\n` +
          `Site: ${siteConfig.displayName}\n` +
          `Checked URLs: ${(siteConfig.urls || []).join(', ')}\n` +
          `Checked Domains: ${siteConfig.aliases.join(', ')}`;
        cookiePreview.style.display = 'block';
        exportBtn.disabled = false;
        return;
      }

      // Format cookies for Playwright
      const formattedCookies = cookies.map(c => {
        // Convert sameSite to Playwright format
        let sameSite = 'Lax';
        if (c.sameSite === 'no_restriction') sameSite = 'None';
        else if (c.sameSite === 'strict') sameSite = 'Strict';
        else if (c.sameSite === 'lax') sameSite = 'Lax';
        
        return {
          name: c.name,
          value: c.value,
          domain: c.domain.startsWith('.') ? c.domain.slice(1) : c.domain,
          path: c.path,
          secure: c.secure,
          httpOnly: c.httpOnly,
          sameSite: sameSite
        };
      });

      // Show preview
      const essentialCookies = formattedCookies.filter(c =>
        siteConfig.essentialCookies.includes(c.name)
      );

      const missingRequired = (siteConfig.requiredCookies || []).filter(
        (name) => !formattedCookies.some((c) => c.name === name)
      );
      if (missingRequired.length > 0) {
        showStatus(
          `Missing required login cookie(s): ${missingRequired.join(', ')}. Login to ${siteConfig.displayName} first.`,
          'error'
        );
        exportBtn.disabled = false;
        return;
      }

      cookiePreview.textContent =
        `Site: ${siteConfig.displayName}\n` +
        `Found ${formattedCookies.length} cookies\n` +
        `Essential: ${essentialCookies.length ? essentialCookies.map(c => c.name).join(', ') : '(none matched)'}`;
      cookiePreview.style.display = 'block';

      showStatus('Sending cookies to dashboard...', 'info');

      // Send to backend
      const formData = new FormData();
      formData.append('cookies', JSON.stringify(formattedCookies));

      const response = await fetch(`${apiUrl}/api/website-sources/${sourceId}`, {
        method: 'PUT',
        body: formData
      });

      if (response.ok) {
        const data = await response.json();
        showStatus(`✓ Cookies exported successfully! ${formattedCookies.length} cookies saved.`, 'success');
      } else {
        const error = await response.text();
        showStatus(`Error: ${response.status} - ${error}`, 'error');
      }
    } catch (error) {
      showStatus(`Error: ${error.message}`, 'error');
    } finally {
      exportBtn.disabled = false;
    }
  });

  function showStatus(message, type) {
    statusDiv.textContent = message;
    statusDiv.className = `status ${type}`;
  }

  // Check if on a supported site
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const url = tabs[0]?.url || '';
    if (url.includes('bossjob.us')) {
      showStatus('Ready to export cookies from bossjob.us', 'success');
    } else if (url.includes('linkedin.com')) {
      showStatus('Ready to export cookies from linkedin.com', 'success');
    } else {
      showStatus('Navigate to bossjob.us or linkedin.com and login first', 'info');
    }
  });
});
