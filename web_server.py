# web_server.py
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates # For serving index.html easily

import json
import time
import os # For path joining

# --- Logging ---
def log_server(msg: str):
    print(f"[WEB_SERVER] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

# --- FastAPI App Initialization ---
app = FastAPI()

# Determine base directory for path construction
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Mount static files (CSS, JS)
# The path "/static" in the URL will map to the "frontend" directory.
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "frontend")), name="static")

# Setup Jinja2 templates
# Templates are expected to be in a "templates" directory.
# For this setup, our index.html is in "frontend", so we point templates to "frontend".
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "frontend"))


# --- WebSocket Connection Manager ---
connected_clients: set[WebSocket] = set()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    log_server(f"Client connected: {websocket.client}. Total clients: {len(connected_clients)}")
    try:
        while True:
            data = await websocket.receive_text() # Keep listening for potential client messages
            log_server(f"Received message from {websocket.client}: {data} (ignoring for now)")
            # Example: if you want to implement client-side ping or specific commands
            # if data == "ping":
            #     await websocket.send_text("pong")
    except WebSocketDisconnect:
        log_server(f"Client disconnected: {websocket.client} (gracefully). Total clients: {len(connected_clients)}")
    except Exception as e:
        log_server(f"WebSocket error for {websocket.client}: {e}. Connection will be closed.")
    finally: # Ensure client is removed from the set on any exit from the try block
        if websocket in connected_clients:
            connected_clients.remove(websocket)
            log_server(f"Removed client {websocket.client} from active set. Total clients: {len(connected_clients)}")


# --- HTTP Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def get_root(request: Request): # Add request: Request for Jinja2
    log_server("Serving root HTML page via Jinja2 template.")
    # The "request" object is required by Jinja2Templates.
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/display")
async def display_data_endpoint(request: Request):
    try:
        data = await request.json()
        log_server(f"Received data for display via POST /api/display: {json.dumps(data, indent=2)}")
        
        if not isinstance(data, dict) or "type" not in data or "payload" not in data:
            log_server("Invalid data format received for display. Type or Payload missing.")
            # For now, we will still attempt to broadcast if clients are connected,
            # but ideally, you'd return a 400 error to the caller (tool_executor).
            # This endpoint's primary job is broadcasting; validation of content structure
            # for specific display types (graph, markdown) should ideally happen
            # before calling this endpoint OR be very robust here if this is the sole validator.
            # For now, let's assume the caller (tool_executor) will try to send a valid structure.

        clients_to_send = list(connected_clients) # Iterate over a copy
        if not clients_to_send:
            log_server("No WebSocket clients connected to broadcast display data.")
            return {"status": "received_but_no_clients", "message": "Data received, but no display clients are currently connected."}

        broadcast_count = 0
        for client_ws in clients_to_send:
            try:
                await client_ws.send_json(data)
                log_server(f"Sent data to client {client_ws.client}")
                broadcast_count += 1
            except Exception as e:
                log_server(f"Error sending data to client {client_ws.client}: {e}. Will attempt to remove.")
                # This client might be dead, attempt removal (disconnect handler should also catch this)
                if client_ws in connected_clients:
                    connected_clients.remove(client_ws)
        
        if broadcast_count > 0:
            return {"status": "success", "message": f"Data received and broadcasted to {broadcast_count} client(s)."}
        else:
            # This case implies clients_to_send was not empty, but all failed to send.
            return {"status": "error", "message": "Data received, but failed to broadcast to any initially connected clients."}

    except json.JSONDecodeError:
        log_server("Error: POST /api/display received non-JSON data.")
        # FastAPI should automatically return a 422 Unprocessable Entity for this.
        # If we catch it, we can customize, but often it's better to let FastAPI handle request body validation.
        return {"status": "error", "message": "Invalid JSON payload in request body."} # Should be a 400/422
    except Exception as e:
        log_server(f"Critical error processing /api/display: {e}")
        return {"status": "error", "message": f"Internal server error: {str(e)}"} # Should be a 500


# --- Main Guard ---
if __name__ == "__main__":
    log_server(f"Starting Uvicorn server for web_server.py on http://localhost:8001. Serving static from: {os.path.join(BASE_DIR, 'frontend')}")
    uvicorn.run("web_server:app", host="0.0.0.0", port=8001, reload=True)