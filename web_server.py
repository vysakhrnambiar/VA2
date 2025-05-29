# web_server.py
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import json
import time
import os

# Import manual call routes
from manual_call_routes import router as manual_call_router

# --- Logging ---
def log_server(msg: str):
    print(f"[WEB_SERVER] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

# --- FastAPI App Initialization ---
app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "frontend")), name="static")
app.mount("/static/manual_call", StaticFiles(directory=os.path.join(BASE_DIR, "frontend/manual_call")), name="static_manual_call")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "frontend"))

# Include manual call router
app.include_router(manual_call_router)

# --- WebSocket Connection Manager ---
connected_clients: set[WebSocket] = set()

async def broadcast_to_clients(message: dict):
    """Helper function to broadcast a JSON message to all connected WebSocket clients."""
    if not connected_clients:
        log_server(f"No WebSocket clients connected to broadcast message: {message.get('type')}")
        return 0 # Return count of clients messaged
    
    clients_to_send = list(connected_clients) # Iterate over a copy
    broadcast_count = 0
    for client_ws in clients_to_send:
        try:
            await client_ws.send_json(message)
            log_server(f"Sent message type '{message.get('type')}' to client {client_ws.client}")
            broadcast_count += 1
        except WebSocketDisconnect: # Client disconnected before we could send
            log_server(f"Client {client_ws.client} disconnected during broadcast. Removing.")
            if client_ws in connected_clients: # Ensure it's still there before removing
                connected_clients.remove(client_ws)
        except Exception as e:
            log_server(f"Error sending message to client {client_ws.client}: {e}. Removing.")
            if client_ws in connected_clients: # Ensure it's still there before removing
                connected_clients.remove(client_ws)
    return broadcast_count


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    log_server(f"Client connected: {websocket.client}. Total clients: {len(connected_clients)}")
    try:
        while True:
            data = await websocket.receive_text()
            log_server(f"Received message from {websocket.client}: {data} (currently ignored by server)")
    except WebSocketDisconnect:
        log_server(f"Client disconnected: {websocket.client} (gracefully).")
    except Exception as e:
        log_server(f"WebSocket error for {websocket.client}: {e}. Connection will be closed.")
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
            log_server(f"Removed client {websocket.client} from active set. Total clients: {len(connected_clients)}")

# --- HTTP Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def get_root(request: Request):
    log_server("Serving root HTML page via Jinja2 template.")
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/display") # Existing endpoint for displaying charts/markdown
async def display_data_endpoint(request: Request):
    try:
        data = await request.json()
        log_server(f"Received data for display via POST /api/display: Type '{data.get('type')}'")
        
        if not isinstance(data, dict) or "type" not in data or "payload" not in data:
            log_server("Invalid data format for /api/display. Type or Payload missing.")
            return {"status": "error", "message": "Invalid payload format."} # Should be 400/422

        sent_count = await broadcast_to_clients(data)
        
        if sent_count > 0:
            return {"status": "success", "message": f"Data received and broadcasted to {sent_count} client(s)."}
        elif not connected_clients: # Check again after broadcast attempt
             return {"status": "received_but_no_clients", "message": "Data received, but no display clients are currently connected."}
        else: # Had clients, but all failed
            return {"status": "error", "message": "Data received, but failed to broadcast to any initially connected clients."}

    except json.JSONDecodeError:
        log_server("Error: POST /api/display received non-JSON data.")
        return {"status": "error", "message": "Invalid JSON payload in request body."}
    except Exception as e:
        log_server(f"Critical error processing /api/display: {e}")
        return {"status": "error", "message": f"Internal server error: {str(e)}"}


# --- New Endpoints for Phase 4 ---

@app.post("/api/ui_status_update")
async def ui_status_update_endpoint(request: Request):
    """
    Receives status updates (e.g., connection status from openai_client)
    and broadcasts them to all connected WebSocket clients.
    """
    try:
        data = await request.json() # Expecting {"type": "connection_status", "status": {"connection": "...", "message": "..."}}
        log_server(f"Received POST /api/ui_status_update: Type '{data.get('type')}', Connection Status '{data.get('status', {}).get('connection')}'")

        if not isinstance(data, dict) or "type" not in data or "status" not in data:
            log_server("Invalid data format for /api/ui_status_update.")
            return {"status": "error", "message": "Invalid payload format for UI status update."}

        sent_count = await broadcast_to_clients(data)
        
        if sent_count > 0:
            return {"status": "success", "message": f"UI status update broadcasted to {sent_count} client(s)."}
        elif not connected_clients:
             return {"status": "received_but_no_clients", "message": "UI status update received, but no clients connected."}
        else:
            return {"status": "error", "message": "UI status update received, but failed to broadcast."}
            
    except json.JSONDecodeError:
        log_server("Error: POST /api/ui_status_update received non-JSON data.")
        return {"status": "error", "message": "Invalid JSON payload."}
    except Exception as e:
        log_server(f"Critical error processing /api/ui_status_update: {e}")
        return {"status": "error", "message": f"Internal server error: {str(e)}"}


@app.post("/api/notify_call_update_available")
async def notify_call_update_available_endpoint(request: Request):
    """
    Receives notification that a call task update is available (from db_monitor_thread in main.py)
    and broadcasts this to all connected WebSocket clients.
    """
    try:
        data = await request.json() # Expecting {"type": "new_call_update_available", "contact_name": "...", "status_summary": "..."}
        log_server(f"Received POST /api/notify_call_update_available for Contact: '{data.get('contact_name')}'")

        if not isinstance(data, dict) or \
           data.get("type") != "new_call_update_available" or \
           "contact_name" not in data or \
           "status_summary" not in data: # Add more specific validation for this type
            log_server("Invalid data format for /api/notify_call_update_available.")
            return {"status": "error", "message": "Invalid payload format for call update notification."}

        sent_count = await broadcast_to_clients(data)

        if sent_count > 0:
            return {"status": "success", "message": f"Call update notification broadcasted to {sent_count} client(s)."}
        elif not connected_clients:
             return {"status": "received_but_no_clients", "message": "Call update notification received, but no clients connected."}
        else:
            return {"status": "error", "message": "Call update notification received, but failed to broadcast."}

    except json.JSONDecodeError:
        log_server("Error: POST /api/notify_call_update_available received non-JSON data.")
        return {"status": "error", "message": "Invalid JSON payload."}
    except Exception as e:
        log_server(f"Critical error processing /api/notify_call_update_available: {e}")
        return {"status": "error", "message": f"Internal server error: {str(e)}"}

# --- Main Guard ---
if __name__ == "__main__":
    log_server(f"Starting Uvicorn server for web_server.py on http://localhost:8001.")
    uvicorn.run("web_server:app", host="0.0.0.0", port=8001, reload=True) # reload=True is good for dev