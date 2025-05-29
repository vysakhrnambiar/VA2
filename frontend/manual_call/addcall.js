// Manual Call Request Form JavaScript
document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('call-request-form');
    const statusMessage = document.getElementById('submission-status');
    
    // Form validation
    function validateForm() {
        const contactName = document.getElementById('contact-name').value.trim();
        const phoneNumber = document.getElementById('phone-number').value.trim();
        const companyName = document.getElementById('company-name').value.trim();
        const callPurpose = document.getElementById('call-purpose').value.trim();
        
        if (!contactName) {
            showErrorMessage('Customer name is required');
            return false;
        }
        
        if (!phoneNumber) {
            showErrorMessage('Customer phone number is required');
            return false;
        }
        
        // Basic phone number validation - only digits, at least 7 characters
        if (!/^\d{7,}$/.test(phoneNumber)) {
            showErrorMessage('Please enter a valid phone number (digits only, at least 7 numbers)');
            return false;
        }
        
        if (!callPurpose) {
            showErrorMessage('Call objective is required');
            return false;
        }
        
        return true;
    }
    
    // Replace [COMPANY] placeholders with actual company name
    function updateObjectiveWithCompany() {
        let companyName = document.getElementById('company-name').value.trim();
        const callPurpose = document.getElementById('call-purpose');
        
        // Use DTC Executive Office as default if company name is empty
        if (!companyName) {
            companyName = "DTC Executive Office";
        }
        
        if (callPurpose.value.includes('[COMPANY]')) {
            callPurpose.value = callPurpose.value.replace(/\[COMPANY\]/g, companyName);
        }
    }
    
    // Show error message
    function showErrorMessage(message) {
        statusMessage.textContent = message;
        statusMessage.className = 'status-message status-error';
    }
    
    // Show success message
    function showSuccessMessage(message) {
        statusMessage.textContent = message;
        statusMessage.className = 'status-message status-success';
    }
    
    // Handle form submission
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        // Clear previous status messages
        statusMessage.className = 'status-message hidden';
        
        // Validate form
        if (!validateForm()) {
            return;
        }
        
        // Update objective text with company name
        updateObjectiveWithCompany();
        
        try {
            // Collect form data
            const formData = new FormData(form);
            
            // Submit the form using fetch API
            const response = await fetch('/api/manual_call', {
                method: 'POST',
                body: formData
            });
            
            const result = await response.json();
            
            if (response.ok) {
                // Show success message
                showSuccessMessage(result.message || 'Call request created successfully!');
                
                // Reset form after successful submission
                form.reset();
                
                // Set default text for call purpose
                document.getElementById('call-purpose').value =
                    `This is a follow-up call from [COMPANY]. The customer previously requested information or a call back. The agent should:
1. Introduce themselves as calling from [COMPANY]
2. Ask about the customer's dental issues or concerns
3. Discuss possible treatment options if appropriate
4. Offer to schedule an appointment if the customer is interested
5. Thank the customer for their time`;
                
                // Update with company name
                updateObjectiveWithCompany();
                
                // Set default selection for urgency
                document.getElementById('urgency').value = 'medium';
            } else {
                // Show error message
                showErrorMessage(result.detail || 'Failed to create call request. Please try again.');
            }
        } catch (error) {
            console.error('Error submitting form:', error);
            showErrorMessage('An error occurred while submitting the form. Please try again.');
        }
    });
    
    // Update objective text when company name changes
    document.getElementById('company-name').addEventListener('change', updateObjectiveWithCompany);
    document.getElementById('company-name').addEventListener('blur', updateObjectiveWithCompany);
    
    // Reset status message when form is cleared
    form.addEventListener('reset', () => {
        statusMessage.className = 'status-message hidden';
        
        // Reset call purpose to default value after form reset
        setTimeout(() => {
            // Reset company name
            document.getElementById('company-name').value = 'Al Hind Dental clinic';
            
            document.getElementById('call-purpose').value =
                `This is a follow-up call from [COMPANY]. The customer previously requested information or a call back. The agent should:
1. Introduce themselves as calling from [COMPANY]
2. Ask about the customer's dental issues or concerns
3. Discuss possible treatment options if appropriate
4. Offer to schedule an appointment if the customer is interested
5. Thank the customer for their time`;
            
            // Reset urgency to default value
            document.getElementById('urgency').value = 'medium';
            
            // Update the template with the company name
            updateObjectiveWithCompany();
        }, 10);
    });
    
    // Initialize the form by updating objective with company name on page load
    updateObjectiveWithCompany();
});