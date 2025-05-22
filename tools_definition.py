# tools_definition.py

# --- Tool Names (Constants) ---
END_CONVERSATION_TOOL_NAME = "end_conversation_and_listen_for_wakeword"
SEND_EMAIL_SUMMARY_TOOL_NAME = "send_email_discussion_summary"
RAISE_TICKET_TOOL_NAME = "raise_ticket_for_missing_knowledge"
GET_BOLT_KB_TOOL_NAME = "get_bolt_knowledge_base_info"
GET_DTC_KB_TOOL_NAME = "get_dtc_knowledge_base_info"

# --- Tool Definitions ---

TOOL_END_CONVERSATION = {
    "type": "function",
    "name": END_CONVERSATION_TOOL_NAME,
    "description": (
        "Call this function when the current conversation topic or user's immediate query has been fully addressed, "
        "and the assistant should return to a passive state, listening for its wake word to be reactivated. "
        "Also use this if the user explicitly ends the conversation (e.g., 'thank you, that's all', 'goodbye', "
        "'stop listening', 'go to sleep')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "A brief reason why the conversation is ending or why the assistant is returning to listen mode. "
                    "For example: 'User said goodbye', 'User's query resolved', 'User requested to stop'."
                )
            }
        },
        "required": ["reason"]
    }
}

TOOL_SEND_EMAIL_SUMMARY = {
    "type": "function",
    "name": SEND_EMAIL_SUMMARY_TOOL_NAME,
    "description": "Sends an email summary of the current or recent discussion to pre-configured recipients. Use this when the user explicitly asks to email the conversation or key points discussed.",
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "A concise and relevant subject line for the email, summarizing the content."
            },
            "body_summary": {
                "type": "string",
                "description": "The main content of the email, summarizing the key points of the discussion. This will be formatted into an HTML email."
            }
        },
        "required": ["subject", "body_summary"]
    }
}

TOOL_RAISE_TICKET = {
    "type": "function",
    "name": RAISE_TICKET_TOOL_NAME,
    "description": "If the user asks a question and the information is not found in the available knowledge bases (Bolt KB, DTC KB), first ask the user if they want to raise a ticket to request this information be added. If they agree, call this function to send an email to the admin.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_query": { # Renamed for clarity
                "type": "string",
                "description": "The specific question or topic the user asked about that was not found in the knowledge base."
            },
            "additional_context": { # Renamed for clarity
                "type": "string",
                "description": "Any relevant context from the conversation that might help the admin understand the user's need for this missing information. Be concise."
            }
        },
        "required": ["user_query"] # additional_context can be optional
    }
}

TOOL_GET_BOLT_KB = {
    "type": "function",
    "name": GET_BOLT_KB_TOOL_NAME,
    "description": "Retrieves information specifically about Bolt services, operations, or data from the Bolt knowledge base. Use this when the user's query is clearly about Bolt. Provide the specific user query or topic to search for.",
    "parameters": {
        "type": "object",
        "properties": {
            "query_topic": { # Renamed for clarity
                "type": "string",
                "description": "The specific topic or keywords from the user's question about Bolt to search for in the knowledge base (e.g., 'Bolt revenue yesterday', 'Bolt promotions', 'Bolt total orders March'). Be specific."
            }
        },
        "required": ["query_topic"]
    }
}

TOOL_GET_DTC_KB = {
    "type": "function",
    "name": GET_DTC_KB_TOOL_NAME,
    "description": "Retrieves information specifically about DTC services, limousine operations, or general DTC data from the DTC knowledge base. Use this when the user's query is clearly about DTC or limousines. Provide the specific user query or topic to search for.",
    "parameters": {
        "type": "object",
        "properties": {
            "query_topic": { # Renamed for clarity
                "type": "string",
                "description": "The specific topic or keywords from the user's question about DTC to search for in the knowledge base (e.g., 'DTC fleet size', 'DTC airport transfer revenue', 'DTC contact'). Be specific."
            }
        },
        "required": ["query_topic"]
    }
}

# List of all tools to be passed to OpenAI
ALL_TOOLS = [
    TOOL_END_CONVERSATION,
    TOOL_SEND_EMAIL_SUMMARY,
    TOOL_RAISE_TICKET,
    TOOL_GET_BOLT_KB,
    TOOL_GET_DTC_KB
]