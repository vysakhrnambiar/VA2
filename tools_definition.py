# tools_definition.py

# --- Tool Names (Constants) ---
END_CONVERSATION_TOOL_NAME = "end_conversation_and_listen_for_wakeword"
SEND_EMAIL_SUMMARY_TOOL_NAME = "send_email_discussion_summary"
RAISE_TICKET_TOOL_NAME = "raise_ticket_for_missing_knowledge"
GET_BOLT_KB_TOOL_NAME = "get_bolt_knowledge_base_info"
GET_DTC_KB_TOOL_NAME = "get_dtc_knowledge_base_info"
DISPLAY_ON_INTERFACE_TOOL_NAME = "display_on_interface" # New tool name
GET_TAXI_IDEAS_FOR_TODAY_TOOL_NAME = "get_taxi_ideas_for_today"
GENERAL_GOOGLE_SEARCH_TOOL_NAME = "general_google_search"

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
            "user_query": {
                "type": "string",
                "description": "The specific question or topic the user asked about that was not found in the knowledge base."
            },
            "additional_context": {
                "type": "string",
                "description": "Any relevant context from the conversation that might help the admin understand the user's need for this missing information. Be concise."
            }
        },
        "required": ["user_query"]
    }
}

TOOL_GET_BOLT_KB = {
    "type": "function",
    "name": GET_BOLT_KB_TOOL_NAME,
    "description": "Retrieves information specifically about Bolt services, operations, or data from the Bolt knowledge base. Use this when the user's query is clearly about Bolt. Provide the specific user query or topic to search for.",
    "parameters": {
        "type": "object",
        "properties": {
            "query_topic": {
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
            "query_topic": {
                "type": "string",
                "description": "The specific topic or keywords from the user's question about DTC to search for in the knowledge base (e.g., 'DTC fleet size', 'DTC airport transfer revenue', 'DTC contact'). Be specific."
            }
        },
        "required": ["query_topic"]
    }
}

# New Tool Definition for Displaying on Interface
TOOL_DISPLAY_ON_INTERFACE = {
    "type": "function",
    "name": DISPLAY_ON_INTERFACE_TOOL_NAME,
    "description": (
        "Sends structured data to a connected web interface for visual display. "
        "Use this tool when a visual representation (text, markdown, or graph) would enhance the user's understanding. "
        "The web interface can display markdown (including tables) and various chart types (bar, line, pie)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "display_type": {
                "type": "string",
                "enum": ["markdown", "graph_bar", "graph_line", "graph_pie"],
                "description": "The type of content to display. 'markdown' for text, lists, and tables. 'graph_bar', 'graph_line', or 'graph_pie' for charts."
            },
            "title": {
                "type": "string",
                "description": "An optional title for the content. For graphs, this is the chart title. For markdown, it can be a main heading (e.g., '## My Title')."
            },
            "data": {
                "type": "object",
                "description": "The actual data payload, structured according to the 'display_type'. See examples for each type.",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "For 'markdown' display_type: The full markdown string. Can include headings, lists, bold/italic text, and tables (e.g., '| Header1 | Header2 |\\n|---|---|\\n| Val1 | Val2 |')."
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "For graph types: An array of strings for the X-axis labels (bar, line) or segment labels (pie)."
                    },
                    "datasets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Name of this dataset (e.g., 'Sales Q1', 'Temperature')."},
                                "values": {"type": "array", "items": {"type": "number"}, "description": "Array of numerical data points corresponding to 'labels'."}
                            },
                            "required": ["label", "values"]
                        },
                        "description": "For graph types: An array of dataset objects. Each object contains a label for the dataset and its corresponding values. For pie charts, typically only one dataset is used."
                    },
                    "options": { # LLM can suggest general options
                        "type": "object",
                        "properties": {
                            "animated": {"type": "boolean", "description": "Suggest if the graph should be animated (if supported by the frontend). Default: true."},
                            "x_axis_label": {"type": "string", "description": "Optional label for the X-axis of bar or line charts."},
                            "y_axis_label": {"type": "string", "description": "Optional label for the Y-axis of bar or line charts."}
                        },
                        "description": "Optional: General display options or hints for the frontend, like animation or axis labels for graphs."
                    }
                },
                "description_detailed_examples": ( # Custom field for our reference, not for OpenAI schema
                    "Example for 'markdown': data: { 'content': '# Report Title\\n- Point 1\\n- Point 2\\n| Col A | Col B |\\n|---|---|\\n| 1 | 2 |' }\n"
                    "Example for 'graph_bar': data: { 'labels': ['Jan', 'Feb'], 'datasets': [{'label': 'Revenue', 'values': [100, 150]}], 'options': {'x_axis_label': 'Month'} }\n"
                    "Example for 'graph_pie': data: { 'labels': ['Slice A', 'Slice B'], 'datasets': [{'label': 'Distribution', 'values': [60, 40]}] }"
                )
            }
        },
        "required": ["display_type", "data"]
        # Depending on display_type, specific fields within 'data' become effectively required.
        # For example, if display_type is 'markdown', data.content is required.
        # If display_type is 'graph_bar', data.labels and data.datasets are required.
        # The LLM needs to be prompted/trained to understand this conditional requirement.
        # We can also add logic in the tool handler to validate this.
    }
}

# --- New Tool Definitions ---
TOOL_GET_TAXI_IDEAS = {
    "type": "function",
    "name": GET_TAXI_IDEAS_FOR_TODAY_TOOL_NAME,
    "description": (
        "Fetches actionable ideas, relevant news, and event information for taxi services in Dubai for the current day. "
        "Use this when specifically asked for daily taxi deployment suggestions, event-based opportunities, local news relevant to transport, "
        "or operational ideas for today. The tool requires the current date."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "current_date": {
                "type": "string",
                "description": "The current date in 'Month DD, YYYY' format (e.g., 'May 24, 2025'). This is mandatory to get relevant information for today."
            },
            "specific_focus": {
                "type": "string",
                "description": "Optional: A specific focus for the ideas, like 'airport demand', 'major sporting events', or 'shopping mall traffic'."
            }
        },
        "required": ["current_date"]
    }
}

TOOL_GENERAL_GOOGLE_SEARCH = {
    "type": "function",
    "name": GENERAL_GOOGLE_SEARCH_TOOL_NAME,
    "description": (
        "Searches the internet using Google for information on general topics, current events, business news, "
        "competitor information, or other subjects not covered by internal knowledge bases. "
        "Primarily for queries related to Dubai, professional contexts, or general knowledge. "
        "Use for questions like 'What is the weather in Dubai today?', 'Latest news on autonomous taxis in UAE', "
        "or 'Who is the CEO of Company X?'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "search_query": {
                "type": "string",
                "description": "The specific and concise query to search on Google. Example: 'current fuel prices in Dubai', 'traffic conditions Sheikh Zayed Road now'."
            }
        },
        "required": ["search_query"]
    }
}


# List of all tools to be passed to OpenAI

# List of all tools to be passed to OpenAI
ALL_TOOLS = [
    TOOL_END_CONVERSATION,
    TOOL_SEND_EMAIL_SUMMARY,
    TOOL_RAISE_TICKET,
    TOOL_GET_BOLT_KB,
    TOOL_GET_DTC_KB,
    TOOL_DISPLAY_ON_INTERFACE, # Add the new tool here
    TOOL_GET_TAXI_IDEAS, # Add new tool
    TOOL_GENERAL_GOOGLE_SEARCH # Add new tool
]

