from fastapi import FastAPI, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import google.oauth2.id_token
from google.auth.transport import requests as google_requests
from google.cloud import firestore
import datetime

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
    If the document does not exist, create it.
    """
    uid = user_token.get("uid")
    if not uid:
        return None
    user_ref = firestore_db.collection("users").document(uid)
    user_doc = user_ref.get()
    if not user_doc.exists:
        user_data = {
            "email": user_token.get("email"),
        }
        user_ref.set(user_data)
    return user_ref

def create_directory(uid: str, name: str, parent_path: str = "/"):
    """
    Creates a new directory for a user.
    
    Args:
        uid: User ID
        name: Directory name
        parent_path: Parent directory path, defaults to root
    
    Returns:
        The newly created directory document reference
    
    Raises:
        ValueError: If directory name is empty or if directory already exists
    """
    name = name.strip()
    if not name:
        raise ValueError("Directory name cannot be empty")
    
    # Ensure parent_path ends with /
    if not parent_path.endswith("/"):
        parent_path += "/"
    
    # Calculate the full path
    path = parent_path + name
    
    # Check if directory already exists for this user at the given path
    existing_dirs = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .where("path", "==", path) \
        .limit(1) \
        .get()
    
    if len(existing_dirs) > 0:
        raise ValueError(f"Directory '{name}' already exists in '{parent_path}'")
    
    # Create the directory document
    dir_data = {
        "name": name,
        "path": path,
        "parent_path": parent_path,
        "user_id": uid,
        "created_at": datetime.datetime.utcnow().isoformat()
    }
    # Add the document to the 'Directories' collection
    doc_ref = firestore_db.collection("Directories").add(dir_data)
    return doc_ref

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # Retrieve the Firebase token from cookies
    id_token_cookie = request.cookies.get("token")
    error_message = None
    user_token = validate_firebase_token(id_token_cookie)
    
    if not user_token:
        return templates.TemplateResponse("main.html", {
            "request": request,
            "user_token": None,
            "error_message": error_message,
            "directories": []
        })
    
    user_ref = get_user(user_token)
    uid = user_token.get("uid")
    
    # Query directories for this user that are direct children of the root (parent_path "/")
    dirs_query = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .where("parent_path", "==", "/") \
        .stream()
    directories = []
    for d in dirs_query:
        dir_data = d.to_dict()
        dir_data["id"] = d.id
        directories.append(dir_data)
    
    return templates.TemplateResponse("main.html", {
        "request": request,
        "user_token": user_token,
        "error_message": error_message,
        "directories": directories
    })

@app.post("/create-directory", response_class=RedirectResponse)
async def create_directory_route(request: Request, dirname: str = Form(...)):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    uid = user_token.get("uid")
    try:
        create_directory(uid, dirname, parent_path="/")
    except ValueError as err:
        # Log the error; in a full app you might pass this error to the UI.
        print(err)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

@app.post("/delete-directory", response_class=RedirectResponse)
async def delete_directory_route(request: Request, directory_id: str = Form(...)):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    uid = user_token.get("uid")
    dir_ref = firestore_db.collection("Directories").document(directory_id)
    dir_doc = dir_ref.get()
    if not dir_doc.exists:
        print("Directory not found")
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    dir_data = dir_doc.to_dict()
    if dir_data.get("user_id") != uid:
        print("Unauthorized deletion attempt")
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    dir_ref.delete()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

@app.post("/signout")
async def signout(request: Request):
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("token")
    return response

@app.get("/update-user", response_class=HTMLResponse)
async def update_form(request: Request):
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
