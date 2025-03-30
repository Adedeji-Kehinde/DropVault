from fastapi import FastAPI, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import google.oauth2.id_token
from google.auth.transport import requests as google_requests
from google.cloud import firestore

app = FastAPI()

# Initialize Firestore client
firestore_db = firestore.Client()

# Request adapter for Firebase token verification
firebase_request_adapter = google_requests.Request()

# Mount static files (CSS, JS, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize Jinja2 templates (assumes templates directory exists)
templates = Jinja2Templates(directory="templates")

def validate_firebase_token(id_token: str):
    """
    Validates the Firebase ID token using google-auth.
    The audience should be set to your Firebase project ID.
    """
    if not id_token:
        return None
    try:
        user_token = google.oauth2.id_token.verify_firebase_token(
            id_token,
            firebase_request_adapter,
            audience="dropvault-1"  # Replace with your actual Firebase project ID
        )
        return user_token
    except ValueError as err:
        print("Token verification error:", err)
        return None

def get_user(user_token: dict):
    """
    Retrieves the user document from Firestore using the uid from the token.
    If the document does not exist, create it with a default root directory.
    """
    uid = user_token.get("uid")
    if not uid:
        return None
    user_ref = firestore_db.collection("users").document(uid)
    user_doc = user_ref.get()
    if not user_doc.exists:
        # Create default user document with a default root directory.
        default_data = {
            "email": user_token.get("email"),
            "root_directory": {
                "path": "/",
                "directories": [],  # List to hold subdirectory details
                "files": []         # List to hold file metadata
            }
        }
        user_ref.set(default_data)
        return user_ref
    return user_ref

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # Retrieve the Firebase token from cookies
    id_token_cookie = request.cookies.get("token")
    error_message = None
    user_token = validate_firebase_token(id_token_cookie)
    
    # If token validation fails, render the template without user info
    if not user_token:
        return templates.TemplateResponse("main.html", {
            "request": request,
            "user_token": None,
            "error_message": error_message,
            "user_info": None
        })
    
    # Retrieve (or create) the user document from Firestore
    user_ref = get_user(user_token)
    user_info = user_ref.get().to_dict() if user_ref else None
    
    return templates.TemplateResponse("main.html", {
        "request": request,
        "user_token": user_token,
        "error_message": error_message,
        "user_info": user_info
    })

@app.post("/signout")
async def signout(request: Request):
    """
    Sign out the user by clearing the token cookie and redirecting to the home page.
    The client-side firebase-login.js should also call this endpoint on sign out.
    """
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("token")
    return response

@app.get("/update-user", response_class=HTMLResponse)
async def update_form(request: Request):
    # Get token from cookies and validate it
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse("/")
    
    user_ref = get_user(user_token)
    user_info = user_ref.get().to_dict() if user_ref else None
    
    return templates.TemplateResponse("update.html", {
        "request": request,
        "user_token": user_token,
        "error_message": None,
        "user_info": user_info
    })

@app.post("/update-user", response_class=RedirectResponse)
async def update_form_post(request: Request, name: str = Form(...), age: int = Form(...)):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse("/")
    
    user_ref = get_user(user_token)
    user_ref.update({"name": name, "age": age})
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080, reload=True)
