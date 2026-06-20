(function () {
    'use strict';

    const statusEl = document.getElementById('status');
    const checkBtn = document.getElementById('check-btn');
    const graceBtn = document.getElementById('grace-btn');
    const copyBtn = document.getElementById('copy-btn');
    const addressEl = document.getElementById('eth-address');
    const chainSelect = document.getElementById('chain-select');
    const tokenSelect = document.getElementById('token-select');
    const tierSelect = document.getElementById('tier-select');
    const chainInfo = document.getElementById('chain-info');
    const qrCode = document.getElementById('qr-code');
    const metamaskLink = document.getElementById('metamask-link');

    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    const hasWindowEthereum = typeof window.ethereum !== 'undefined';

    const configEl = document.getElementById('portal-config');
    let CONFIG = {};
    try {
        CONFIG = configEl ? JSON.parse(configEl.textContent) : {};
    } catch (e) {
        CONFIG = {};
    }
    const pageConfigEl = document.getElementById('page-config');
    if (pageConfigEl) {
        try { Object.assign(CONFIG, JSON.parse(pageConfigEl.textContent)); } catch (e) {}
    } else if (typeof PAGE_CONFIG !== 'undefined') {
        Object.assign(CONFIG, PAGE_CONFIG);
    }

    let pollTimer = null;
    let statusTimer = null;
    let networkTimer = null;
    let pollDelay = CONFIG.pollInterval || 3000;
    let pollInFlight = false;
    let isPageVisible = !document.hidden;
    let chains = {};
    let currentChain = CONFIG ? CONFIG.currentChain : null;
    let currentTier = CONFIG ? CONFIG.tierIndex : 0;
    let currentToken = CONFIG ? CONFIG.token : 'ETH';

    function showStatus(message, type) {
        if (!statusEl) return;
        statusEl.textContent = message;
        statusEl.className = 'status ' + (type || 'info');
    }

    function hideStatus() {
        if (!statusEl) return;
        statusEl.className = 'status';
        statusEl.textContent = '';
    }

    function formatBytes(bytes) {
        if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(2) + ' GB';
        if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
        if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return bytes + ' B';
    }

    function formatSeconds(totalSeconds) {
        if (totalSeconds <= 0) return '已到期';
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;
        if (hours > 0) {
            return hours + '小时 ' + minutes + '分钟';
        }
        return minutes + '分钟 ' + seconds + '秒';
    }

    function selectedTier() {
        const cfg = chains[currentChain];
        if (!cfg) return null;
        const tiers = cfg.tiers;
        if (!tiers || currentTier < 0 || currentTier >= tiers.length) {
            currentTier = 0;
        }
        return tiers[currentTier];
    }

    function updateChainUI() {
        const cfg = chains[currentChain];
        if (!cfg) return;
        if (!qrCode) return;
        const address = CONFIG ? CONFIG.ethAddress : addressEl.textContent;
        const tier = selectedTier();
        if (!tier) return;

        qrCode.src = '/api/qr?chain=' + encodeURIComponent(currentChain) +
            '&address=' + encodeURIComponent(address) +
            '&token=' + encodeURIComponent(currentToken) +
            '&t=' + Date.now();
        qrCode.onerror = function() {
            qrCode.alt = '二维码加载失败，请刷新页面';
            qrCode.style.display = 'none';
        };

        chainInfo.textContent = '';
        const nameEl = document.createElement('strong');
        nameEl.textContent = cfg.name;
        chainInfo.appendChild(nameEl);

        const amountUsd = tier.amount_usd || 0;
        chainInfo.appendChild(document.createTextNode(' · 支付 $' + amountUsd.toFixed(2) + ' USD'));

        chainInfo.appendChild(document.createTextNode(' · 获得 '));
        const quotaEl = document.createElement('strong');
        quotaEl.textContent = formatBytes(tier.quota_bytes);
        chainInfo.appendChild(quotaEl);
        chainInfo.appendChild(document.createTextNode(' · 预计 ' + (cfg.block_time * 3) + ' 秒确认'));

        const mmUri = 'https://metamask.app.link/send/' + address +
            '@' + cfg.chain_id + '?value=' + (CONFIG.amountWei || '0');
        metamaskLink.href = mmUri;

        showStatus('已切换到 ' + cfg.name + ' · ' + formatBytes(tier.quota_bytes) + ' 档位', 'info');
    }

    function populateTierSelect() {
        if (!tierSelect) return;
        const cfg = chains[currentChain];
        if (!cfg) return;
        tierSelect.innerHTML = '';
        cfg.tiers.forEach(function (tier, idx) {
            const option = document.createElement('option');
            option.value = idx;
            const label = tier.amount_usd ? '$' + tier.amount_usd.toFixed(2) + ' USD' : (tier.amount_eth || '');
            option.textContent = formatBytes(tier.quota_bytes) + ' · ' + label;
            tierSelect.appendChild(option);
        });
        if (currentTier >= cfg.tiers.length) {
            currentTier = 0;
        }
        tierSelect.value = currentTier;
    }

    async function loadChains() {
        try {
            const res = await fetch('/api/chains');
            if (!res.ok) throw new Error('加载网络列表失败');
            chains = await res.json();

            if (chainSelect) {
                chainSelect.innerHTML = '';
                const recommendedGroup = document.createElement('optgroup');
                recommendedGroup.label = '推荐网络';
                const otherGroup = document.createElement('optgroup');
                otherGroup.label = '其他网络';

                Object.keys(chains).forEach(function (id) {
                    const cfg = chains[id];
                    const option = document.createElement('option');
                    option.value = id;
                    option.textContent = cfg.icon + ' ' + cfg.name;
                    if (cfg.recommended) {
                        recommendedGroup.appendChild(option);
                    } else {
                        otherGroup.appendChild(option);
                    }
                });

                chainSelect.appendChild(recommendedGroup);
                chainSelect.appendChild(otherGroup);
                chainSelect.value = currentChain;
            }

            populateTierSelect();
            updateChainUI();
            updateWalletVisibility();
        } catch (err) {
            showStatus('无法加载支付网络：' + err.message, 'error');
        }
    }

    async function payWithWallet() {
        if (!window.ethereum) {
            showStatus('未检测到钱包插件，请安装 MetaMask 或其他钱包插件', 'error');
            return;
        }
        try {
            const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
            if (!accounts || accounts.length === 0) {
                showStatus('请先解锁钱包', 'error');
                return;
            }
            const tier = selectedTier();
            if (!tier) return;
            showStatus('请在钱包中确认交易…', 'info');
            try {
                const chainIdHex = await window.ethereum.request({ method: 'eth_chainId' });
                const currentChainId = parseInt(chainIdHex, 16);
                if (currentChainId !== CONFIG.chainId) {
                    showStatus('请先切换到正确的网络（' + chains[currentChain]?.name + '）', 'error');
                    return;
                }
            } catch (e) {
                showStatus('无法验证网络，请确保已切换到 ' + (chains[currentChain]?.name || '正确网络'), 'error');
                return;
            }
            const txHash = await window.ethereum.request({
                method: 'eth_sendTransaction',
                params: [{
                    from: accounts[0],
                    to: CONFIG.ethAddress,
                    value: '0x' + BigInt(CONFIG.amountWei || '0').toString(16),
                    chainId: '0x' + CONFIG.chainId.toString(16)
                }]
            });
            showStatus('交易已提交，等待确认…', 'info');
            startPaymentPolling();
        } catch (err) {
            if (err.code === 4001) {
                showStatus('交易已取消', 'error');
            } else {
                showStatus('交易失败：' + err.message, 'error');
            }
        }
    }

    const walletBtn = document.getElementById('wallet-btn');
    if (walletBtn) {
        walletBtn.addEventListener('click', payWithWallet);
    }

    function updateWalletVisibility() {
        var qrSection = document.getElementById('qr-section');
        var walletSection = document.getElementById('wallet-section');
        var walletBtnEl = document.getElementById('wallet-btn');

        if (hasWindowEthereum && !isMobile) {
            if (qrSection) qrSection.style.display = 'none';
            if (walletSection) walletSection.style.display = '';
            if (walletBtnEl) walletBtnEl.style.display = '';
        } else {
            if (qrSection) qrSection.style.display = '';
            if (walletSection) walletSection.style.display = 'none';
            if (walletBtnEl) walletBtnEl.style.display = 'none';
        }
    }

    function buildIndexUrl() {
        return '/?chain=' + encodeURIComponent(currentChain) +
            '&tier=' + encodeURIComponent(currentTier) +
            '&token=' + encodeURIComponent(currentToken);
    }

    function schedulePoll(delay) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
        if (!isPageVisible) return;
        pollTimer = setTimeout(function () {
            checkPayment(true);
        }, delay);
    }

    function startPaymentPolling() {
        pollDelay = CONFIG.pollInterval || 3000;
        schedulePoll(pollDelay);
    }

    function stopPaymentPolling() {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
    }

    async function checkPayment(auto) {
        if (pollInFlight) return;
        pollInFlight = true;

        try {
            if (!auto && checkBtn) {
                checkBtn.disabled = true;
                showStatus('', 'info');
                statusEl.textContent = '';
                var spinner = document.createElement('span');
                spinner.className = 'spinner';
                spinner.setAttribute('aria-hidden', 'true');
                statusEl.appendChild(spinner);
                statusEl.appendChild(document.createTextNode('正在检查支付状态…'));
            }

            const url = '/api/check-payment?chain=' + encodeURIComponent(currentChain) +
                '&tier=' + encodeURIComponent(currentTier);
            var controller = null;
            var fetchTimeout = null;
            if (typeof AbortController !== 'undefined') {
                controller = new AbortController();
                fetchTimeout = setTimeout(function () {
                    controller.abort();
                }, CONFIG.fetchTimeout || 20000);
            }

            const res = await fetch(url, {
                method: 'POST',
                signal: controller ? controller.signal : undefined
            });
            if (fetchTimeout) clearTimeout(fetchTimeout);

            if (!res.ok) {
                throw new Error('网络请求失败：' + res.status);
            }
            const data = await res.json();

            if (data.paid) {
                if (data.status === 'grace') {
                    showStatus('当前处于宽限期，正在跳转…', 'info');
                } else {
                    showStatus('支付已确认，正在跳转…', 'success');
                }
                pollDelay = CONFIG.pollInterval || 3000;
                stopPaymentPolling();
                setTimeout(function () {
                    window.location.href = '/success';
                }, CONFIG.redirectDelay || 1000);
                return;
            } else if (!auto) {
                showStatus('尚未检测到支付，请完成支付后等待确认。', 'error');
            }

            if (auto) {
                pollDelay = CONFIG.pollInterval || 3000;
                schedulePoll(pollDelay);
            }
        } catch (err) {
            if (!auto) {
                showStatus('检查失败：' + err.message, 'error');
            }
            if (auto) {
                pollDelay = Math.min(CONFIG.pollMaxInterval || 30000, pollDelay * 2);
                schedulePoll(pollDelay);
            }
        } finally {
            pollInFlight = false;
            if (!auto && checkBtn) {
                checkBtn.disabled = false;
            }
        }
    }

    async function simulatePayment() {
        if (!CONFIG || !CONFIG.devMode) {
            showStatus('当前环境不支持模拟支付。', 'error');
            return;
        }

        try {
            const url = '/api/simulate-payment?chain=' + encodeURIComponent(currentChain) +
                '&tier=' + encodeURIComponent(currentTier);
            const res = await fetch(url, { method: 'POST' });
            if (!res.ok) {
                const data = await res.json().catch(function() { return {}; });
                throw new Error(data.error || '请求失败：' + res.status);
            }
            showStatus('模拟支付成功，正在跳转…', 'success');
            setTimeout(function () {
                window.location.href = '/success';
            }, CONFIG.redirectDelay || 1000);
        } catch (err) {
            showStatus('模拟支付失败：' + err.message, 'error');
        }
    }

    async function activateGrace() {
        if (!graceBtn) return;
        try {
            graceBtn.disabled = true;
            showStatus('正在开启临时免费上网…', 'info');
            const res = await fetch('/api/activate-grace', { method: 'POST' });
            const data = await res.json().catch(function() { return {}; });
            if (!res.ok || !data.ok) {
                throw new Error(data.error || '开启失败：' + res.status);
            }
            showStatus('已开启临时免费上网（' + formatBytes(data.quota_bytes) + ' / ' + formatSeconds(CONFIG.graceDurationSeconds || 300) + '）', 'success');
            setTimeout(function () {
                window.location.reload();
            }, CONFIG.redirectDelay || 1000);
        } catch (err) {
            showStatus('开启失败：' + err.message, 'error');
            if (graceBtn) graceBtn.disabled = false;
        }
    }

    if (chainSelect) {
        chainSelect.addEventListener('change', function () {
            const selected = chainSelect.value;
            if (selected !== currentChain) {
                currentChain = selected;
                populateTierSelect();
                window.location.href = buildIndexUrl();
            }
        });
    }

    if (tierSelect) {
        tierSelect.addEventListener('change', function () {
            const selected = parseInt(tierSelect.value, 10);
            if (!isNaN(selected) && selected !== currentTier) {
                currentTier = selected;
                window.location.href = buildIndexUrl();
            }
        });
    }

    if (tokenSelect) {
        tokenSelect.addEventListener('change', function () {
            const selected = tokenSelect.value;
            if (selected !== currentToken) {
                currentToken = selected;
                window.location.href = buildIndexUrl();
            }
        });
    }

    if (checkBtn) {
        checkBtn.addEventListener('click', function () {
            checkPayment(false);
        });
    }

    if (graceBtn) {
        graceBtn.addEventListener('click', activateGrace);
    }

    if (copyBtn && addressEl) {
        function fallbackCopy(text) {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.setSelectionRange(0, ta.value.length);
            ta.select();
            try {
                document.execCommand('copy');
                showStatus('地址已复制到剪贴板', 'success');
            } catch (e) {
                showStatus('复制失败', 'error');
            }
            document.body.removeChild(ta);
        }

        copyBtn.addEventListener('click', function () {
            var text = addressEl.textContent;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function () {
                    showStatus('地址已复制到剪贴板', 'success');
                }, function () {
                    fallbackCopy(text);
                });
            } else {
                fallbackCopy(text);
            }
        });
    }

    // ------------------------------------------------------------------
    // Success page: poll /api/status and /generate_204.
    // ------------------------------------------------------------------
    function updateUsageBar(used, quota) {
        const bar = document.getElementById('usage-bar');
        if (!bar || quota <= 0) return;
        const pct = Math.min(100, Math.max(0, (used / quota) * 100));
        bar.value = pct;
        bar.classList.remove('warning', 'danger');
        if (pct >= 90) {
            bar.classList.add('danger');
        } else if (pct >= 70) {
            bar.classList.add('warning');
        }
    }

    async function pollStatus() {
        try {
            const res = await fetch('/api/status');
            if (!res.ok) return;
            const data = await res.json();

            const remainingBytesEl = document.getElementById('remaining-bytes');
            const remainingTimeEl = document.getElementById('remaining-time');
            const renewHint = document.getElementById('renew-hint');

            if (data.status !== 'paid' && data.status !== 'grace') {
                window.location.href = '/';
                return;
            }

            const remainingBytes = data.remaining_bytes || 0;
            const limitTime = data.status === 'paid' ? data.paid_until : data.grace_until;
            const remainingSeconds = Math.max(0, limitTime - Math.floor(Date.now() / 1000));

            const h1 = document.querySelector('h1');
            if (data.status === 'grace') {
                if (h1) h1.textContent = '当前处于宽限期';
            } else {
                if (h1) h1.textContent = '支付成功';
            }

            if (remainingBytesEl) remainingBytesEl.textContent = formatBytes(remainingBytes);
            if (remainingTimeEl) remainingTimeEl.textContent = formatSeconds(remainingSeconds);
            updateUsageBar(data.used_bytes, data.quota_bytes);

            if (renewHint) {
                if (remainingBytes < (CONFIG.lowBytesThreshold || 52428800) || remainingSeconds < (CONFIG.lowTimeThreshold || 600)) {
                    renewHint.classList.remove('hidden');
                } else {
                    renewHint.classList.add('hidden');
                }
            }
        } catch (err) {
            var statusText = document.getElementById('network-status');
            if (statusText) statusText.textContent = '连接中断，请检查网络…';
        }
    }

    function checkNetwork() {
        const statusText = document.getElementById('network-status');
        fetch('/generate_204', { cache: 'no-store', redirect: 'manual' })
            .then(function (res) {
                if (res.status === 204) {
                    if (statusText) {
                        statusText.textContent = '网络已连通，您可以关闭此窗口。';
                        statusText.className = 'success-text';
                    }
                } else {
                    if (statusText) {
                        statusText.textContent = '网络状态检测中，请稍候…';
                        statusText.className = 'warning-text';
                    }
                }
            })
            .catch(function () {
                if (statusText) {
                    statusText.textContent = '网络状态检测中，请稍候…';
                    statusText.className = '';
                }
            });
    }

    function clearTimers() {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
        if (statusTimer) clearInterval(statusTimer);
        statusTimer = null;
        if (networkTimer) clearInterval(networkTimer);
        networkTimer = null;
    }

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            isPageVisible = false;
            stopPaymentPolling();
        } else {
            isPageVisible = true;
            if (currentChain && !document.getElementById('remaining-bytes')) {
                startPaymentPolling();
            }
        }
    });

    if (document.getElementById('remaining-bytes')) {
        // On success page.
        pollStatus();
        statusTimer = setInterval(pollStatus, CONFIG.statusPollInterval || 3000);
        checkNetwork();
        networkTimer = setInterval(checkNetwork, CONFIG.networkCheckInterval || 10000);
    } else if (currentChain) {
        // On payment page.
        loadChains();
        startPaymentPolling();
    }

    window.addEventListener('pagehide', clearTimers);
    window.addEventListener('beforeunload', clearTimers);
})();
