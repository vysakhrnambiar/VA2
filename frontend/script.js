// frontend/script.js
document.addEventListener('DOMContentLoaded', () => {
    const wsStatusElement = document.getElementById('ws-status'); // For the small dot indicator
    const displayArea = document.getElementById('display-area');
    let websocket = null;
    let chartInstance = null;
    let contentTimeoutId = null;
    let logoIdleAnimationId = null;

    // --- New elements for Phase 4 ---
    let connectionStatusBanner = null; // Will be created dynamically
    let callUpdateNotificationArea = null; // Will be created dynamically

    const IDLE_STATE_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

    function createNotificationElements() {
        // Connection Status Banner (persistent at the bottom or top)
        if (!document.getElementById('connection-status-banner')) {
            connectionStatusBanner = document.createElement('div');
            connectionStatusBanner.id = 'connection-status-banner';
            connectionStatusBanner.className = 'notification-banner'; // General styling for banners
            // connectionStatusBanner.style.display = 'none'; // Initially hidden
            document.body.appendChild(connectionStatusBanner); // Append to body to overlay other content
        } else {
            connectionStatusBanner = document.getElementById('connection-status-banner');
        }

        // Call Update Notification Area (e.g., corner icon/toast)
        if (!document.getElementById('call-update-notification-area')) {
            callUpdateNotificationArea = document.createElement('div');
            callUpdateNotificationArea.id = 'call-update-notification-area';
            callUpdateNotificationArea.className = 'notification-banner call-update-banner'; // Specific styling
            // callUpdateNotificationArea.style.display = 'none'; // Initially hidden
            document.body.appendChild(callUpdateNotificationArea);
        } else {
            callUpdateNotificationArea = document.getElementById('call-update-notification-area');
        }
    }
    
    function updateWsStatusIndicator(statusText, cssClass) { // Renamed for clarity
        if (wsStatusElement) {
            wsStatusElement.textContent = statusText; // For accessibility, though it's small
            wsStatusElement.className = ''; 
            wsStatusElement.classList.add(cssClass);
        }
    }

    // --- New functions for Phase 4 Banners ---
    function showConnectionStatusBanner(message, type) { // type can be 'connected', 'disconnected', 'error'
        if (!connectionStatusBanner) createNotificationElements(); // Ensure it exists
        
        connectionStatusBanner.textContent = message;
        connectionStatusBanner.className = 'notification-banner'; // Reset classes
        if (type === 'connected') {
            connectionStatusBanner.classList.add('status-connected-banner');
            // Optionally hide after a few seconds if connected
            setTimeout(() => {
                 if (connectionStatusBanner.classList.contains('status-connected-banner')) { // Check if still connected msg
                    connectionStatusBanner.style.opacity = '0';
                    setTimeout(() => connectionStatusBanner.style.display = 'none', 500);
                 }
            }, 5000); // Hide after 5 seconds
        } else if (type === 'disconnected') {
            connectionStatusBanner.classList.add('status-disconnected-banner');
        } else { // error or other
            connectionStatusBanner.classList.add('status-error-banner');
        }
        connectionStatusBanner.style.display = 'block';
        connectionStatusBanner.style.opacity = '1';
    }

    function hideConnectionStatusBanner() {
        if (connectionStatusBanner) {
            connectionStatusBanner.style.opacity = '0';
            setTimeout(() => connectionStatusBanner.style.display = 'none', 500);
        }
    }

    function showCallUpdateNotification(contactName, summary) {
        if (!callUpdateNotificationArea) createNotificationElements(); // Ensure it exists

        // Simple text for now, can be enhanced with icons, dismiss button etc.
        callUpdateNotificationArea.innerHTML = `🔔 Update on call to <strong>${contactName}</strong>: ${summary.substring(0,50)}...`;
        callUpdateNotificationArea.style.display = 'block';
        callUpdateNotificationArea.style.opacity = '1';
        
        // Optional: Auto-hide after some time, or require user interaction to dismiss
        // setTimeout(hideCallUpdateNotification, 15000); // Hide after 15 seconds
    }

    function hideCallUpdateNotification() {
        if (callUpdateNotificationArea) {
            callUpdateNotificationArea.style.opacity = '0';
            setTimeout(() => callUpdateNotificationArea.style.display = 'none', 500);
        }
    }
    // --- End of New functions for Phase 4 Banners ---

    function clearAllDynamicContent(withAnimation = false) { // Unchanged
        if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
        if (logoIdleAnimationId) { cancelAnimationFrame(logoIdleAnimationId); logoIdleAnimationId = null; }
        if (withAnimation && displayArea.children.length > 0) {
            Array.from(displayArea.children).forEach(child => child.classList.add('animate-fade-out'));
            setTimeout(() => { displayArea.innerHTML = ''; }, 1000);
        } else {
            displayArea.innerHTML = '';
        }
        // When content is cleared, also hide any persistent call update notifications
        hideCallUpdateNotification(); 
    }
    
    function resetContentTimeout() { // Unchanged
        if (contentTimeoutId) clearTimeout(contentTimeoutId);
        if (displayArea.querySelector('#active-content-wrapper')) {
             contentTimeoutId = setTimeout(showIdleState, IDLE_STATE_TIMEOUT_MS);
        }
    }

    function showIdleState() { // Unchanged
        clearAllDynamicContent(true);
        setTimeout(() => {
            const idleContent = document.createElement('div'); /* ... */ 
            idleContent.id = 'idle-state-content'; idleContent.classList.add('animate-fade-in');
            const logoImg = document.createElement('img'); logoImg.id = 'animated-logo-idle'; logoImg.src = '/static/logo.png'; logoImg.alt = 'Voice Assistant Logo'; logoImg.classList.add('animate-float', 'animate-pulse'); idleContent.appendChild(logoImg);
            const titleElement = document.createElement('h2'); titleElement.textContent = 'Ready to assist'; titleElement.classList.add('animate-fade-in'); idleContent.appendChild(titleElement);
            const subtitleElement = document.createElement('p'); subtitleElement.id = 'status-message'; subtitleElement.classList.add('animate-fade-in');
            updateStatusMessage(subtitleElement); idleContent.appendChild(subtitleElement);
            displayArea.appendChild(idleContent);
            let scale = 1; let scaleDirection = 0.002; const minScale = 0.95; const maxScale = 1.05;
            function animateLogo() {
                scale += scaleDirection; if (scale > maxScale || scale < minScale) { scaleDirection *= -1; scale = Math.max(minScale, Math.min(maxScale, scale)); }
                const currentLogo = document.getElementById('animated-logo-idle');
                if (currentLogo) { currentLogo.style.transform = `scale(${scale})`; logoIdleAnimationId = requestAnimationFrame(animateLogo); }
                else { if (logoIdleAnimationId) cancelAnimationFrame(logoIdleAnimationId); logoIdleAnimationId = null; }
            }
            animateLogo();
            if (contentTimeoutId) clearTimeout(contentTimeoutId);
        }, 1000);
    }
    
    function updateStatusMessage(element) { // Unchanged
        if (!element) { element = document.getElementById('status-message'); if (!element) return; }
        if (websocket && websocket.readyState === WebSocket.OPEN) {
            element.textContent = 'Waiting for your command.'; element.classList.remove('disconnected-message');
        } else {
            element.textContent = 'Connection issues. Attempting to reconnect...'; element.classList.add('disconnected-message');
        }
    }
    
    function displayActiveContent(elementProvider) { // Unchanged
        clearAllDynamicContent(true);
        setTimeout(() => {
            const activeContentWrapper = document.createElement('div'); activeContentWrapper.id = 'active-content-wrapper'; activeContentWrapper.classList.add('animate-fade-in');
            const contentElement = elementProvider(); activeContentWrapper.appendChild(contentElement);
            displayArea.appendChild(activeContentWrapper);
            resetContentTimeout();
        }, 1000);
    }

    function renderMarkdown(payload) { /* ... Unchanged ... */ 
        displayActiveContent(() => {
            const markdownContainer = document.createElement('div'); markdownContainer.classList.add('markdown-content');
            let htmlOutput = ""; let mainTitleTextFromPayload = "";
            if (payload.title && payload.title.trim() !== "") { mainTitleTextFromPayload = payload.title.trim(); htmlOutput += marked.parse(mainTitleTextFromPayload.startsWith("#") ? mainTitleTextFromPayload : `<h2>${mainTitleTextFromPayload}</h2>`); }
            if (payload.content && payload.content.trim() !== "") {
                let contentToParse = payload.content.trim();
                if (mainTitleTextFromPayload !== "") { const firstLineOfContent = contentToParse.split('\n')[0].trim(); const normalizedMainTitle = mainTitleTextFromPayload.replace(/^#+\s*/, '').toLowerCase(); const normalizedFirstLineContent = firstLineOfContent.replace(/^#+\s*/, '').toLowerCase(); if (normalizedFirstLineContent === normalizedMainTitle) { const lines = contentToParse.split('\n'); lines.shift(); while (lines.length > 0 && lines[0].trim() === "") lines.shift(); contentToParse = lines.join('\n'); } }
                if (contentToParse.trim() !== "") htmlOutput += marked.parse(contentToParse);
            } else if (htmlOutput === "") { htmlOutput += marked.parse("<em>No specific content provided.</em>"); }
            markdownContainer.innerHTML = htmlOutput; return markdownContainer;
        });
    }
    function renderGraph(type, payload) { /* ... Unchanged ... */ 
        displayActiveContent(() => {
            const graphOuterContainer = document.createElement('div'); graphOuterContainer.classList.add('graph-content');
            if (payload.title) { const titleElement = document.createElement('h2'); titleElement.classList.add('chart-title'); titleElement.textContent = payload.title; graphOuterContainer.appendChild(titleElement); }
            const canvas = document.createElement('canvas'); canvas.style.height = 'clamp(300px, 50vh, 450px)'; canvas.style.width = '100%'; graphOuterContainer.appendChild(canvas);
            const ctx = canvas.getContext('2d');
            const datasets = payload.datasets.map(ds => ({ label: ds.label, data: ds.values, backgroundColor: type === 'graph_pie' ? ['#3b82f6', '#60a5fa', '#93c5fd', '#bfdbfe', '#dbeafe', '#eff6ff'].slice(0, ds.values.length) : 'rgba(59, 130, 246, 0.3)', borderColor: type === 'graph_pie' ? '#100f24' : 'rgba(59, 130, 246, 1)', borderWidth: type === 'graph_pie' ? 2 : 1.5, tension: type === 'graph_line' ? 0.3 : undefined, /* ... other dataset options ... */ }));
            let chartTypeJS; switch(type) { case 'graph_bar': chartTypeJS = 'bar'; break; case 'graph_line': chartTypeJS = 'line'; break; case 'graph_pie': chartTypeJS = 'pie'; break; default: const errorDiv = document.createElement('div'); errorDiv.innerHTML = marked.parse(`<h2>Error</h2><p>Unknown graph type: ${type}</p>`); return errorDiv;}
            const chartData = { labels: payload.labels, datasets: datasets };
            Chart.defaults.color = '#9ca3af'; Chart.defaults.borderColor = '#374151'; Chart.defaults.font.family = "'Space Grotesk', 'Noto Sans', sans-serif";
            const chartOptions = { responsive: true, maintainAspectRatio: false, animation: payload.options?.animated !== undefined ? payload.options.animated : { duration: 800, easing: 'easeInOutQuart' }, scales: {}, plugins: { title: { display: false }, legend: { position: 'bottom', display: (payload.datasets.length > 1 && type !== 'graph_pie') || (type === 'graph_pie' && payload.labels.length > 1), labels: { color: '#d1d5db', padding: 15, font: {size: 13} } }, tooltip: { backgroundColor: 'rgba(31, 29, 61, 0.9)', titleColor: '#f0f0ff', bodyColor: '#d0d0f0', padding: 12, cornerRadius: 3, titleFont: { weight: 'bold', size: 14 }, bodyFont: { size: 13 }, boxPadding: 5 } } };
            if (type === 'graph_bar' || type === 'graph_line') { chartOptions.scales.x = { title: { display: !!payload.options?.x_axis_label, text: payload.options?.x_axis_label, color: '#d1d5db', font:{size:13, weight:'500'} }, grid: { color: '#21204b', drawBorder: false }, ticks: { color: '#9ca3af', font:{size:12} } }; chartOptions.scales.y = { title: { display: !!payload.options?.y_axis_label, text: payload.options?.y_axis_label, color: '#d1d5db', font:{size:13, weight:'500'} }, beginAtZero: true, grid: { color: '#21204b', drawBorder: false }, ticks: { color: '#9ca3af', font:{size:12}, callback: function(value) { if (value >= 1000000) return (value / 1000000) + 'M'; if (value >= 1000) return (value / 1000) + 'K'; return value; } } }; }
            if (chartInstance) chartInstance.destroy(); chartInstance = new Chart(ctx, { type: chartTypeJS, data: chartData, options: chartOptions });
            return graphOuterContainer;
        });
    }

    function connectWebSocket() {
        const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${wsProtocol}//${window.location.host}/ws`;
        
        if (websocket && (websocket.readyState === WebSocket.OPEN || websocket.readyState === WebSocket.CONNECTING)) {
            return;
        }
        
        websocket = new WebSocket(wsUrl);
        updateWsStatusIndicator('Connecting...', 'status-error'); // Update small dot
        // Don't show full banner on initial connect attempt, only on actual disconnect/error

        websocket.onopen = () => {
            updateWsStatusIndicator('Connected', 'status-connected'); // Update small dot
            console.log("WebSocket connection established");
            showConnectionStatusBanner("Agent connected to OpenAI.", "connected"); // Show banner
            updateStatusMessage(); 
        };

        websocket.onmessage = (event) => {
            console.log("Message from server: ", event.data);
            try {
                const messageData = JSON.parse(event.data);
                const type = messageData.type;
                // Payload can be directly messageData.payload OR messageData.status for connection messages
                const payload = messageData.payload || messageData.status || messageData; // Be flexible

                if (!type) {
                    console.error("Invalid message: 'type' missing:", messageData);
                    renderMarkdown({title: "Data Error", content: "Received malformed data (no type)."});
                    return;
                }

                // --- Phase 4: Handle new message types ---
                if (type === 'connection_status') {
                    if (payload && payload.connection === 'connected') {
                        updateWsStatusIndicator('Connected', 'status-connected');
                        showConnectionStatusBanner(payload.message || "Agent reconnected.", "connected");
                    } else if (payload && payload.connection === 'disconnected') {
                        updateWsStatusIndicator('Disconnected', 'status-disconnected');
                        showConnectionStatusBanner(payload.message || "Agent disconnected. Attempting to reconnect...", "disconnected");
                    } else { // error or unknown connection status
                        updateWsStatusIndicator('Error', 'status-error');
                        showConnectionStatusBanner(payload.message || "Connection issue.", "error");
                    }
                    updateStatusMessage(); // Update idle state message if visible
                    return; // Handled this message type
                } else if (type === 'new_call_update_available') {
                    if (payload && payload.contact_name && payload.status_summary) {
                        showCallUpdateNotification(payload.contact_name, payload.status_summary);
                    } else {
                        console.warn("Malformed 'new_call_update_available' payload:", payload);
                    }
                    return; // Handled this message type
                }
                // --- End of Phase 4 Handling ---
                
                // Existing display logic for markdown/graphs
                if (!payload && (type === 'markdown' || type.startsWith('graph_'))) {
                    console.error("Invalid message: 'payload' missing for display type:", messageData);
                    renderMarkdown({title: "Data Error", content: "Received malformed data (no payload for display)."});
                    return;
                }

                if (type === 'markdown') {
                    renderMarkdown(payload);
                } else if (type.startsWith('graph_')) {
                    renderGraph(type, payload);
                } else {
                    console.warn("Received unknown display type (not connection/call update):", type);
                    // Optionally display raw for unknown types if needed for debugging
                }
            } catch (e) {
                console.error("Failed to parse message or render:", e, "Raw data:", event.data);
                // Display a generic error on the main display area if parsing fails
                displayActiveContent(() => {
                    const errorContainer = document.createElement('div');
                    errorContainer.classList.add('markdown-content');
                    errorContainer.innerHTML = `<h2>Display Error</h2>
                                                <p><em>An error occurred processing server message.</em></p>
                                                <pre style="background-color: #1f2937; color: #e5e7eb; padding: 10px; border-radius: 4px;">Data: ${event.data}</pre>`;
                    return errorContainer;
                });
            }
        };

        websocket.onclose = (event) => {
            updateWsStatusIndicator('Disconnected', 'status-disconnected'); // Update small dot
            console.log("WebSocket connection closed", event);
            // Don't show banner immediately from onclose if onmessage for disconnect already showed it.
            // The _notify_frontend_disconnect from client's on_close should trigger the banner via 'connection_status' message.
            updateStatusMessage(); 
            setTimeout(connectWebSocket, self.RECONNECT_DELAY_SECONDS ? self.RECONNECT_DELAY_SECONDS * 1000 : 5000); // Use configured delay
        };

        websocket.onerror = (event) => {
            updateWsStatusIndicator('Error', 'status-error'); // Update small dot
            console.error("WebSocket error observed:", event);
            // Similar to onclose, rely on client sending 'connection_status' for banner.
            updateStatusMessage();
            // Ensure close is called to trigger reconnect logic if error doesn't auto-close
             if (websocket && websocket.readyState !== WebSocket.CLOSED && websocket.readyState !== WebSocket.CLOSING) {
                websocket.close();
            } else if (!websocket || websocket.readyState === WebSocket.CLOSED) {
                 setTimeout(connectWebSocket, self.RECONNECT_DELAY_SECONDS ? self.RECONNECT_DELAY_SECONDS * 1000 : 5000);
            }
        };
    }
    
    // Initial setup
    createNotificationElements(); // Create banner divs once
    showIdleState();
    connectWebSocket();
});