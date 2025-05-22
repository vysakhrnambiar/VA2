// frontend/script.js

document.addEventListener('DOMContentLoaded', () => {
    const wsStatusElement = document.getElementById('ws-status');
    const displayArea = document.getElementById('display-area');
    let websocket = null;
    let chartInstance = null; // To keep track of an existing chart

    function updateWsStatus(statusText, cssClass) {
        if (wsStatusElement) {
            wsStatusElement.textContent = statusText;
            wsStatusElement.className = ''; // Clear existing classes
            wsStatusElement.classList.add(cssClass);
        }
    }

    function clearDisplayArea() {
        if (displayArea) {
            displayArea.innerHTML = ''; // Clear previous content
        }
        if (chartInstance) {
            chartInstance.destroy(); // Destroy previous chart instance
            chartInstance = null;
        }
    }

    function addElementToDisplay(element) {
        if (displayArea) {
            const placeholder = displayArea.querySelector('.placeholder');
            if (placeholder) {
                placeholder.remove();
            }
            displayArea.appendChild(element);
            element.scrollIntoView({ behavior: 'smooth', block: 'end' });
        }
    }
    
    function renderMarkdown(payload) {
        clearDisplayArea();
        const markdownContainer = document.createElement('div');
        markdownContainer.classList.add('markdown-content');
        
        let htmlContent = "";
        if (payload.title) {
            // Marked.js will convert '#' to h1, '##' to h2 etc.
            // Ensure the title is treated as a heading if not already.
            let titleMarkdown = payload.title;
            if (!payload.title.startsWith("#")) {
                titleMarkdown = `<h2>${payload.title}</h2>`; // Or use a specific heading level
                 htmlContent += marked.parse(titleMarkdown);
            } else {
                 htmlContent += marked.parse(titleMarkdown);
            }
           
        }
        if (payload.content) {
            htmlContent += marked.parse(payload.content);
        } else {
            htmlContent += marked.parse("_No content provided for markdown display._");
        }
        markdownContainer.innerHTML = htmlContent;
        addElementToDisplay(markdownContainer);
    }

    function renderGraph(type, payload) {
        clearDisplayArea();
        const graphContainer = document.createElement('div');
        graphContainer.classList.add('graph-content');
        
        if (payload.title) {
            const titleElement = document.createElement('h2');
            titleElement.classList.add('chart-title');
            titleElement.textContent = payload.title;
            graphContainer.appendChild(titleElement);
        }

        const canvas = document.createElement('canvas');
        // It's good to give canvas an ID for potential future reference, though not strictly needed here
        // canvas.id = `chart-${Date.now()}`; 
        graphContainer.appendChild(canvas);
        addElementToDisplay(graphContainer);

        const ctx = canvas.getContext('2d');
        
        // Map LLM's "values" to Chart.js's "data"
        const datasets = payload.datasets.map(ds => ({
            label: ds.label,
            data: ds.values, // Chart.js uses 'data' for the numerical values
            // We can add default styling or derive from payload.options later
            // backgroundColor: 'rgba(75, 192, 192, 0.2)', 
            // borderColor: 'rgba(75, 192, 192, 1)',
            // borderWidth: 1
        }));

        let chartType;
        switch(type) {
            case 'graph_bar': chartType = 'bar'; break;
            case 'graph_line': chartType = 'line'; break;
            case 'graph_pie': chartType = 'pie'; break;
            default:
                console.error("Unknown graph type for Chart.js:", type);
                renderMarkdown({ title: "Error", content: `Cannot render unknown graph type: ${type}` });
                return;
        }
        
        const chartData = {
            labels: payload.labels,
            datasets: datasets
        };

        const chartOptions = {
            responsive: true,
            maintainAspectRatio: true, // Or false if you want to control aspect ratio via CSS/container
            animation: payload.options?.animated !== undefined ? payload.options.animated : true, // Default to animated
            scales: {},
            plugins: {
                title: {
                    display: false, // We are using a separate H2 for title
                    // text: payload.title // Alternatively, use Chart.js internal title
                },
                legend: {
                    position: 'top', // Or 'bottom', 'left', 'right'
                    display: type !== 'graph_pie' // Often legend is more useful for bar/line
                }
            }
        };

        // Add axis labels if provided in options (for bar and line charts)
        if (type === 'graph_bar' || type === 'graph_line') {
            if (payload.options?.x_axis_label) {
                chartOptions.scales.x = { 
                    title: { display: true, text: payload.options.x_axis_label }
                };
            }
            if (payload.options?.y_axis_label) {
                chartOptions.scales.y = { 
                    title: { display: true, text: payload.options.y_axis_label },
                    beginAtZero: true // Common for y-axes
                };
            }
        }


        if (chartInstance) {
            chartInstance.destroy();
        }
        chartInstance = new Chart(ctx, {
            type: chartType,
            data: chartData,
            options: chartOptions
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

        websocket.onopen = function(event) {
            updateWsStatus('Connected', 'status-connected');
            console.log("WebSocket connection established");
            // Optional: Display a system message in the main area
            // addMessageToDisplay("<p class='system-message'><em>Connected to WebSocket server. Waiting for assistant...</em></p>");
        };

        websocket.onmessage = function(event) {
            console.log("Message from server: ", event.data);
            try {
                const messageData = JSON.parse(event.data);
                const type = messageData.type;
                const payload = messageData.payload;

                if (!type || !payload) {
                    console.error("Invalid message structure received:", messageData);
                    renderMarkdown({title: "Error", content: "Received malformed data from server."});
                    return;
                }

                if (type === 'markdown') {
                    renderMarkdown(payload);
                } else if (type.startsWith('graph_')) {
                    renderGraph(type, payload);
                } else {
                    console.warn("Received unknown display type:", type);
                    // Fallback to raw display for unknown types
                    clearDisplayArea();
                    let contentHtml = `<div class="message-header">Unknown Type: <strong>${type}</strong></div>`;
                    contentHtml += `<pre class="raw-json">${JSON.stringify(payload, null, 2)}</pre>`;
                    
                    const unknownItem = document.createElement('div');
                    unknownItem.classList.add('display-item', 'unknown-type');
                    unknownItem.innerHTML = contentHtml;
                    addElementToDisplay(unknownItem);
                }

            } catch (e) {
                console.error("Failed to parse message or render:", e);
                clearDisplayArea();
                const errorItem = document.createElement('div');
                errorItem.classList.add('display-item', 'error-message');
                errorItem.innerHTML = `<p><em>Error processing message from server.</em></p><pre>${event.data}</pre>`;
                addElementToDisplay(errorItem);
            }
        };

        websocket.onclose = function(event) {
            updateWsStatus('Disconnected', 'status-disconnected');
            console.log("WebSocket connection closed", event);
            let reason = "";
            if (event.code) reason += `Code: ${event.code}`;
            if (event.reason) reason += ` Reason: ${event.reason}`;
            if (reason === "") reason = "No specific reason given by server.";
            
            // Optional: Display disconnect message in main area
            // addMessageToDisplay(`<p class="system-message error-message"><em>Disconnected from WebSocket server. ${reason}</em></p>`);
            
            setTimeout(connectWebSocket, 5000);
        };

        websocket.onerror = function(event) {
            updateWsStatus('Error', 'status-error');
            console.error("WebSocket error observed:", event);
            // addMessageToDisplay("<p class='system-message error-message'><em>WebSocket connection error.</em></p>");
            if (websocket.readyState !== WebSocket.CLOSED) {
                websocket.close();
            }
        };
    }
    
    // Initial connection attempt
    connectWebSocket();
});