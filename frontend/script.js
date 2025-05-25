// frontend/script.js
document.addEventListener('DOMContentLoaded', () => {
    const wsStatusElement = document.getElementById('ws-status');
    const displayArea = document.getElementById('display-area');
    let websocket = null;
    let chartInstance = null;
    let contentTimeoutId = null;
    let logoIdleAnimationId = null;

    const IDLE_STATE_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

    function updateWsStatus(statusText, cssClass) {
        if (wsStatusElement) {
            wsStatusElement.textContent = statusText;
            wsStatusElement.className = ''; // Clear existing classes
            wsStatusElement.classList.add(cssClass);
        }
    }

    function clearAllDynamicContent(withAnimation = false) {
        if (chartInstance) {
            chartInstance.destroy();
            chartInstance = null;
        }
        if (logoIdleAnimationId) {
            cancelAnimationFrame(logoIdleAnimationId);
            logoIdleAnimationId = null;
        }
        
        if (withAnimation && displayArea.children.length > 0) {
            // Add fade-out animation to existing content
            Array.from(displayArea.children).forEach(child => {
                child.classList.add('animate-fade-out');
            });
            
            // Wait for animation to complete before clearing
            setTimeout(() => {
                displayArea.innerHTML = ''; // Clear everything inside display-area
            }, 1000); // Match the animation duration (1s)
        } else {
            displayArea.innerHTML = ''; // Clear everything immediately
        }
    }
    
    function resetContentTimeout() {
        if (contentTimeoutId) {
            clearTimeout(contentTimeoutId);
        }
        // Only set timeout if not already in idle state (which showIdleState will be)
        if (displayArea.querySelector('#active-content-wrapper')) {
             contentTimeoutId = setTimeout(showIdleState, IDLE_STATE_TIMEOUT_MS);
        }
    }

    function showIdleState() {
        console.log("Transitioning to idle state");
        clearAllDynamicContent(true); // Clear previous content with animation
        
        // Create a timeout to add the idle content after the fade-out animation completes
        setTimeout(() => {
            const idleContent = document.createElement('div');
            idleContent.id = 'idle-state-content';
            idleContent.classList.add('animate-fade-in');

            const logoImg = document.createElement('img');
            logoImg.id = 'animated-logo-idle';
            logoImg.src = '/static/logo.png';
            logoImg.alt = 'Voice Assistant Logo';
            logoImg.classList.add('animate-float', 'animate-pulse');
            idleContent.appendChild(logoImg);

            const titleElement = document.createElement('h2');
            titleElement.textContent = 'Ready to assist';
            titleElement.classList.add('animate-fade-in');
            idleContent.appendChild(titleElement);

            // Create subtitle element with ID for updating based on connection status
            const subtitleElement = document.createElement('p');
            subtitleElement.id = 'status-message';
            subtitleElement.classList.add('animate-fade-in');
            
            // Set initial message based on current WebSocket status
            updateStatusMessage(subtitleElement);
            
            idleContent.appendChild(subtitleElement);
            
            displayArea.appendChild(idleContent);

            // Combined floating and pulsing animation for the logo
            let scale = 1;
            let scaleDirection = 0.002; // Increased speed for more noticeable effect
            const minScale = 0.95;
            const maxScale = 1.05;

            function animateLogo() {
                scale += scaleDirection;
                if (scale > maxScale || scale < minScale) {
                    scaleDirection *= -1; // Reverse direction
                    scale = Math.max(minScale, Math.min(maxScale, scale)); // Clamp within bounds
                }
                
                const currentLogo = document.getElementById('animated-logo-idle');
                if (currentLogo) { // Check if logo element is still on page
                    // Apply scaling in addition to CSS animations
                    currentLogo.style.transform = `scale(${scale})`;
                    logoIdleAnimationId = requestAnimationFrame(animateLogo);
                } else {
                    if (logoIdleAnimationId) cancelAnimationFrame(logoIdleAnimationId);
                    logoIdleAnimationId = null;
                }
            }
            
            // Start the animation
            animateLogo();
            
            if (contentTimeoutId) clearTimeout(contentTimeoutId); // No timeout when in idle
        }, 1000); // Match the fade-out animation duration
    }
    
    // Function to update the status message based on WebSocket connection
    function updateStatusMessage(element) {
        if (!element) {
            element = document.getElementById('status-message');
            if (!element) return; // Element not found
        }
        
        if (websocket && websocket.readyState === WebSocket.OPEN) {
            element.textContent = 'Waiting for your command. Speak or type your request.';
            element.classList.remove('disconnected-message');
        } else {
            element.textContent = 'Disconnected. Attempting to reconnect...';
            element.classList.add('disconnected-message');
        }
    }
    
    function displayActiveContent(elementProvider) {
        // Use animation when clearing content
        clearAllDynamicContent(true);

        // Create a timeout to add the new content after the fade-out animation completes
        setTimeout(() => {
            const activeContentWrapper = document.createElement('div');
            activeContentWrapper.id = 'active-content-wrapper';
            activeContentWrapper.classList.add('animate-fade-in');
            
            const contentElement = elementProvider(); // Get the markdown div or graph container
            activeContentWrapper.appendChild(contentElement);
            displayArea.appendChild(activeContentWrapper);
            
            resetContentTimeout(); // Start the 5-minute timer for this active content
        }, 1000); // Match the fade-out animation duration
    }

    function renderMarkdown(payload) {
        displayActiveContent(() => {
            const markdownContainer = document.createElement('div');
            markdownContainer.classList.add('markdown-content');
            
            let htmlOutput = "";
            let mainTitleTextFromPayload = "";

            if (payload.title && payload.title.trim() !== "") {
                mainTitleTextFromPayload = payload.title.trim();
                htmlOutput += marked.parse(mainTitleTextFromPayload.startsWith("#") ? mainTitleTextFromPayload : `<h2>${mainTitleTextFromPayload}</h2>`);
            }

            if (payload.content && payload.content.trim() !== "") {
                let contentToParse = payload.content.trim();
                if (mainTitleTextFromPayload !== "") {
                    const firstLineOfContent = contentToParse.split('\n')[0].trim();
                    const normalizedMainTitle = mainTitleTextFromPayload.replace(/^#+\s*/, '').toLowerCase();
                    const normalizedFirstLineContent = firstLineOfContent.replace(/^#+\s*/, '').toLowerCase();
                    if (normalizedFirstLineContent === normalizedMainTitle) {
                        const lines = contentToParse.split('\n'); lines.shift();
                        while (lines.length > 0 && lines[0].trim() === "") lines.shift();
                        contentToParse = lines.join('\n');
                    }
                }
                if (contentToParse.trim() !== "") htmlOutput += marked.parse(contentToParse);
            } else if (htmlOutput === "") {
                htmlOutput += marked.parse("<em>No specific content provided for markdown display.</em>");
            }
            
            markdownContainer.innerHTML = htmlOutput;
            return markdownContainer;
        });
    }

    function renderGraph(type, payload) {
        displayActiveContent(() => {
            const graphOuterContainer = document.createElement('div');
            graphOuterContainer.classList.add('graph-content');

            if (payload.title) {
                const titleElement = document.createElement('h2');
                titleElement.classList.add('chart-title');
                titleElement.textContent = payload.title;
                graphOuterContainer.appendChild(titleElement);
            }

            const canvas = document.createElement('canvas');
            canvas.style.height = 'clamp(300px, 50vh, 450px)'; // Responsive height with min/max
            canvas.style.width = '100%';
            graphOuterContainer.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            
            const datasets = payload.datasets.map(ds => ({
                label: ds.label,
                data: ds.values,
                backgroundColor: type === 'graph_pie' ? ['#3b82f6', '#60a5fa', '#93c5fd', '#bfdbfe', '#dbeafe', '#eff6ff'].slice(0, ds.values.length) : 'rgba(59, 130, 246, 0.3)', // Tailwind blue-500 family
                borderColor: type === 'graph_pie' ? '#100f24' : 'rgba(59, 130, 246, 1)',
                borderWidth: type === 'graph_pie' ? 2 : 1.5,
                tension: type === 'graph_line' ? 0.3 : undefined,
                pointBackgroundColor: type === 'graph_line' ? 'rgba(59, 130, 246, 1)' : undefined,
                pointBorderColor: type === 'graph_line' ? '#fff' : undefined,
                pointHoverBackgroundColor: type === 'graph_line' ? '#fff' : undefined,
                pointHoverBorderColor: type === 'graph_line' ? 'rgba(59, 130, 246, 1)' : undefined,
            }));

            let chartTypeJS;
            switch(type) {
                case 'graph_bar': chartTypeJS = 'bar'; break;
                case 'graph_line': chartTypeJS = 'line'; break;
                case 'graph_pie': chartTypeJS = 'pie'; break;
                default:
                    console.error("Unknown graph type for Chart.js:", type);
                    const errorDiv = document.createElement('div');
                    errorDiv.classList.add('markdown-content');
                    errorDiv.innerHTML = marked.parse(`<h2>Error</h2><p>Cannot render unknown graph type: ${type}</p>`);
                    return errorDiv;
            }
            
            const chartData = { labels: payload.labels, datasets: datasets };

            Chart.defaults.color = '#9ca3af'; // Tailwind gray-400
            Chart.defaults.borderColor = '#374151'; // Tailwind gray-700
            Chart.defaults.font.family = "'Space Grotesk', 'Noto Sans', sans-serif";


            const chartOptions = {
                responsive: true,
                maintainAspectRatio: false,
                animation: payload.options?.animated !== undefined ? payload.options.animated : { duration: 800, easing: 'easeInOutQuart' },
                scales: {},
                plugins: {
                    title: { display: false }, // Using custom H2 for title
                    legend: {
                        position: 'bottom',
                        display: (payload.datasets.length > 1 && type !== 'graph_pie') || (type === 'graph_pie' && payload.labels.length > 1),
                        labels: { color: '#d1d5db', padding: 15, font: {size: 13} }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(31, 29, 61, 0.9)', // Darker tooltip
                        titleColor: '#f0f0ff', 
                        bodyColor: '#d0d0f0',
                        padding: 12, cornerRadius: 3,
                        titleFont: { weight: 'bold', size: 14 },
                        bodyFont: { size: 13 },
                        boxPadding: 5
                    }
                }
            };

            if (type === 'graph_bar' || type === 'graph_line') {
                chartOptions.scales.x = { 
                    title: { display: !!payload.options?.x_axis_label, text: payload.options?.x_axis_label, color: '#d1d5db', font:{size:13, weight:'500'} },
                    grid: { color: '#21204b', drawBorder: false },
                    ticks: { color: '#9ca3af', font:{size:12} }
                };
                chartOptions.scales.y = { 
                    title: { display: !!payload.options?.y_axis_label, text: payload.options?.y_axis_label, color: '#d1d5db', font:{size:13, weight:'500'} },
                    beginAtZero: true,
                    grid: { color: '#21204b', drawBorder: false },
                    ticks: { color: '#9ca3af', font:{size:12}, 
                             callback: function(value) { // Optional: format large numbers
                                if (value >= 1000000) return (value / 1000000) + 'M';
                                if (value >= 1000) return (value / 1000) + 'K';
                                return value;
                             }
                           }
                };
            }

            if (chartInstance) chartInstance.destroy(); // Defensive, should be cleared by clearAllDynamicContent
            chartInstance = new Chart(ctx, { type: chartTypeJS, data: chartData, options: chartOptions });
            
            return graphOuterContainer;
        });
    }

    function connectWebSocket() {
        const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${wsProtocol}//${window.location.host}/ws`;
        
        if (websocket && (websocket.readyState === WebSocket.OPEN || websocket.readyState === WebSocket.CONNECTING)) {
            console.log("WebSocket is already open or connecting.");
            return;
        }
        
        websocket = new WebSocket(wsUrl);
        updateWsStatus('Connecting...', 'status-error');

        websocket.onopen = () => {
            updateWsStatus('Connected', 'status-connected');
            console.log("WebSocket connection established");
            updateStatusMessage(); // Update status message when connected
        };

        websocket.onmessage = (event) => {
            console.log("Message from server: ", event.data);
            try {
                const messageData = JSON.parse(event.data);
                const type = messageData.type;
                const payload = messageData.payload;

                if (!type || !payload) {
                    console.error("Invalid message structure received:", messageData);
                    renderMarkdown({title: "Data Error", content: "Received malformed data from server."});
                    return;
                }

                if (type === 'markdown') {
                    renderMarkdown(payload);
                } else if (type.startsWith('graph_')) {
                    renderGraph(type, payload);
                } else {
                    console.warn("Received unknown display type:", type);
                    displayActiveContent(() => {
                        const unknownContainer = document.createElement('div');
                        unknownContainer.classList.add('markdown-content');
                        unknownContainer.innerHTML = `<h2>Unknown Display Type: <strong>${type}</strong></h2>
                                                     <pre style="background-color: #1f2937; color: #e5e7eb; padding: 10px; border-radius: 4px;">${JSON.stringify(payload, null, 2)}</pre>`;
                        return unknownContainer;
                    });
                }
            } catch (e) {
                console.error("Failed to parse message or render:", e);
                displayActiveContent(() => {
                    const errorContainer = document.createElement('div');
                    errorContainer.classList.add('markdown-content');
                    errorContainer.innerHTML = `<h2>Display Error</h2>
                                                <p><em>An error occurred while trying to display the content.</em></p>
                                                <pre style="background-color: #1f2937; color: #e5e7eb; padding: 10px; border-radius: 4px;">Data: ${event.data}</pre>`;
                    return errorContainer;
                });
            }
        };

        websocket.onclose = (event) => {
            updateWsStatus('Disconnected', 'status-disconnected');
            console.log("WebSocket connection closed", event);
            // Update the status message to show disconnected state
            updateStatusMessage();
            // Do not automatically revert to idle state on disconnect,
            // as user might be looking at content. Reconnection will handle updates.
            setTimeout(connectWebSocket, 5000); // Attempt to reconnect
        };

        websocket.onerror = (event) => {
            updateWsStatus('Error', 'status-error');
            console.error("WebSocket error observed:", event);
            // Update the status message to show error state
            updateStatusMessage();
            // On error, ensure connection is closed to trigger onclose's reconnect logic
            if (websocket.readyState !== WebSocket.CLOSED && websocket.readyState !== WebSocket.CLOSING) {
                websocket.close();
            } else if (websocket.readyState === WebSocket.CLOSED) {
                // If already closed (e.g. server unavailable), still schedule reconnect
                setTimeout(connectWebSocket, 5000);
            }
        };
    }
    
    // Initial setup
    showIdleState();
    connectWebSocket();
});