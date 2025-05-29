// Call Monitoring Dashboard JavaScript
document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const callsTableBody = document.getElementById('calls-table-body');
    const refreshButton = document.getElementById('refresh-button');
    const modal = document.getElementById('call-details-modal');
    const closeModal = document.getElementById('close-modal');
    
    // Initial data load
    loadCallsData();
    
    // Event Listeners
    refreshButton.addEventListener('click', loadCallsData);
    closeModal.addEventListener('click', () => modal.style.display = 'none');
    window.addEventListener('click', (e) => {
        if (e.target === modal) modal.style.display = 'none';
    });
    
    // Fetch and display calls data
    async function loadCallsData() {
        showLoadingState();
        try {
            const response = await fetch('/api/calls');
            if (!response.ok) {
                throw new Error(`Server returned ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            renderCallsTable(data.calls);
        } catch (error) {
            showErrorState(error.message);
            console.error('Error loading calls data:', error);
        }
    }
    
    // Show loading state in table
    function showLoadingState() {
        callsTableBody.innerHTML = `
            <tr class="loading-row">
                <td colspan="8">
                    <div class="loading-spinner"></div>
                    <p>Loading calls data...</p>
                </td>
            </tr>
        `;
    }
    
    // Show error state in table
    function showErrorState(message) {
        callsTableBody.innerHTML = `
            <tr class="error-row">
                <td colspan="8">
                    <p class="error-message">Error loading data: ${message}</p>
                    <button id="retry-button" class="button secondary-button">Retry</button>
                </td>
            </tr>
        `;
        document.getElementById('retry-button').addEventListener('click', loadCallsData);
    }
    
    // Render calls table with data
    function renderCallsTable(calls) {
        if (!calls || calls.length === 0) {
            callsTableBody.innerHTML = `
                <tr>
                    <td colspan="8" class="no-data-message">
                        No call records found. <a href="/addcall">Add a new call</a> to get started.
                    </td>
                </tr>
            `;
            return;
        }
        
        callsTableBody.innerHTML = '';
        calls.forEach(call => {
            const row = document.createElement('tr');
            
            // Format status badge
            const statusClass = call.overall_status ? 
                `status-${call.overall_status.toLowerCase().replace('_', '-')}` : 
                'status-unknown';
            
            // Format dates
            const nextRetryDate = call.next_retry_at ? 
                new Date(call.next_retry_at).toLocaleString() : 'Not scheduled';
            const createdDate = call.created_at ? 
                new Date(call.created_at).toLocaleString() : 'Unknown';
            
            row.innerHTML = `
                <td>${call.id}</td>
                <td>${escapeHtml(call.contact_name || 'Unknown')}</td>
                <td>${escapeHtml(call.phone_number || 'N/A')}</td>
                <td><span class="status-badge ${statusClass}">${formatStatus(call.overall_status)}</span></td>
                <td>${call.retries_attempted || 0} / ${call.max_retries || 3}</td>
                <td>${nextRetryDate}</td>
                <td>${createdDate}</td>
                <td class="action-cell">
                    <button class="action-button view-button" data-call-id="${call.id}">View Details</button>
                </td>
            `;
            
            callsTableBody.appendChild(row);
        });
        
        // Add event listeners to view buttons
        document.querySelectorAll('.view-button').forEach(button => {
            button.addEventListener('click', () => viewCallDetails(button.dataset.callId));
        });
    }
    
    // Format status for display
    function formatStatus(status) {
        if (!status) return 'Unknown';
        return status.replace('_', ' ').toLowerCase()
            .replace(/\b\w/g, l => l.toUpperCase());
    }
    
    // View call details in modal
    async function viewCallDetails(callId) {
        // Show loading in modal
        modal.style.display = 'block';
        document.getElementById('modal-title').textContent = `Call Details (ID: ${callId})`;
        document.getElementById('attempts-container').innerHTML = `
            <div class="loading-spinner"></div>
            <p style="text-align: center; margin-top: 1rem;">Loading call details...</p>
        `;
        
        try {
            const response = await fetch(`/api/call/${callId}/attempts`);
            if (!response.ok) {
                throw new Error(`Server returned ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            renderCallDetails(data.call, data.attempts);
        } catch (error) {
            document.getElementById('attempts-container').innerHTML = `
                <p class="error-message">Error loading call details: ${error.message}</p>
                <button id="retry-details-button" class="button secondary-button">Retry</button>
            `;
            document.getElementById('retry-details-button').addEventListener('click', () => viewCallDetails(callId));
            console.error('Error loading call details:', error);
        }
    }
    
    // Render call details in modal
    function renderCallDetails(call, attempts) {
        // Update call information
        document.getElementById('detail-contact').textContent = call.contact_name || 'Unknown';
        document.getElementById('detail-phone').textContent = call.phone_number || 'N/A';
        
        const statusClass = call.overall_status ? 
            `status-${call.overall_status.toLowerCase().replace('_', '-')}` : 
            'status-unknown';
        document.getElementById('detail-status').innerHTML = 
            `<span class="status-badge ${statusClass}">${formatStatus(call.overall_status)}</span>`;
        
        document.getElementById('detail-attempts').textContent = 
            `${call.retries_attempted || 0} / ${call.max_retries || 3}`;
        
        const nextRetryDate = call.next_retry_at ? 
            new Date(call.next_retry_at).toLocaleString() : 'Not scheduled';
        document.getElementById('detail-next-retry').textContent = nextRetryDate;
        
        const createdDate = call.created_at ? 
            new Date(call.created_at).toLocaleString() : 'Unknown';
        document.getElementById('detail-created').textContent = createdDate;
        
        // Update objectives
        document.getElementById('detail-initial-objective').textContent =
            call.initial_call_objective_description || 'No initial objective set.';
        document.getElementById('detail-current-objective').textContent =
            call.current_call_objective_description || 'No current objective set.';
        document.getElementById('detail-final-summary').textContent =
            call.final_summary_for_main_agent || 'No final summary available yet.';
        
        // Render attempts
        const attemptsContainer = document.getElementById('attempts-container');
        if (!attempts || attempts.length === 0) {
            attemptsContainer.innerHTML = `
                <p class="no-attempts-message">No call attempts have been made yet.</p>
            `;
            return;
        }
        
        attemptsContainer.innerHTML = '';
        attempts.forEach((attempt, index) => {
            const attemptElement = document.createElement('div');
            attemptElement.className = 'attempt-accordion';
            
            // Format dates
            const startedDate = attempt.attempt_started_at ? 
                new Date(attempt.attempt_started_at).toLocaleString() : 'Unknown';
            const endedDate = attempt.attempt_ended_at ? 
                new Date(attempt.attempt_ended_at).toLocaleString() : 'Still in progress';
            
            // Determine status class
            let statusBadge = '';
            if (attempt.attempt_status) {
                const statusClass = `status-${attempt.attempt_status.toLowerCase().replace('_', '-')}`;
                statusBadge = `<span class="status-badge ${statusClass}">${formatStatus(attempt.attempt_status)}</span>`;
            }
            
            attemptElement.innerHTML = `
                <div class="attempt-header" data-attempt-id="${attempt.attempt_id}">
                    <div class="attempt-title">
                        Attempt #${attempt.attempt_number} ${statusBadge}
                    </div>
                    <div class="attempt-time">
                        ${startedDate}
                    </div>
                </div>
                <div class="attempt-content" id="attempt-content-${attempt.attempt_id}">
                    <div class="attempt-info">
                        <h6>Objective for This Attempt</h6>
                        <div class="attempt-info-text">${attempt.objective_for_this_attempt || 'No objective specified.'}</div>
                    </div>
                    
                    <div class="attempt-info">
                        <h6>Attempt Timeline</h6>
                        <div class="attempt-info-text">
                            <strong>Started:</strong> ${startedDate}<br>
                            <strong>Ended:</strong> ${endedDate}<br>
                            <strong>Reason:</strong> ${attempt.end_reason || 'Not specified'}
                        </div>
                    </div>
                    
                    <div class="attempt-info">
                        <h6>Strategist Summary</h6>
                        <div class="attempt-info-text">${attempt.strategist_summary_of_attempt || 'No summary available.'}</div>
                    </div>
                    
                    <div class="attempt-info">
                        <h6>Objective Met Status</h6>
                        <div class="attempt-info-text">${attempt.strategist_objective_met_status_for_attempt || 'Not evaluated.'}</div>
                    </div>
                    
                    <div class="attempt-info">
                        <h6>Strategist Reasoning</h6>
                        <div class="attempt-info-text">${attempt.strategist_reasoning_for_attempt || 'No reasoning provided.'}</div>
                    </div>
                    
                    ${attempt.attempt_error_details ? `
                    <div class="attempt-info">
                        <h6>Error Details</h6>
                        <div class="attempt-info-text error-text">${attempt.attempt_error_details}</div>
                    </div>
                    ` : ''}
                    
                    ${attempt.transcript ? `
                    <div class="attempt-info">
                        <h6>Call Transcript</h6>
                        <button class="toggle-transcript-btn button secondary-button">Show Transcript</button>
                        <div class="transcript-container" style="display: none;">
                            ${formatTranscript(attempt.transcript)}
                        </div>
                    </div>
                    ` : ''}
                </div>
            `;
            
            attemptsContainer.appendChild(attemptElement);
        });
        
        // Add event listeners to attempt headers for accordion functionality
        document.querySelectorAll('.attempt-header').forEach(header => {
            header.addEventListener('click', () => {
                const content = document.getElementById(`attempt-content-${header.dataset.attemptId}`);
                content.classList.toggle('active');
            });
        });
        
        // Add event listeners to transcript toggle buttons
        document.querySelectorAll('.toggle-transcript-btn').forEach(button => {
            button.addEventListener('click', (e) => {
                const transcriptContainer = e.target.nextElementSibling;
                if (transcriptContainer.style.display === 'none') {
                    transcriptContainer.style.display = 'block';
                    e.target.textContent = 'Hide Transcript';
                } else {
                    transcriptContainer.style.display = 'none';
                    e.target.textContent = 'Show Transcript';
                }
            });
        });
        
        // Open the first attempt by default
        if (attempts.length > 0) {
            document.getElementById(`attempt-content-${attempts[0].attempt_id}`).classList.add('active');
        }
    }
    
    // Format transcript for display
    function formatTranscript(transcript) {
        if (!transcript) return 'No transcript available.';
        
        // Basic formatting - can be enhanced further
        return escapeHtml(transcript)
            .replace(/\n/g, '<br>')
            .replace(/Agent:/g, '<strong>Agent:</strong>')
            .replace(/Customer:/g, '<strong>Customer:</strong>');
    }
    
    // Helper function to escape HTML
    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
});