from fastapi import FastAPI, Request, Form, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import google.oauth2.id_token
from google.auth.transport import requests as google_requests
from google.cloud import firestore
import datetime
import local_constants
from io import BytesIO

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
    """
    name = name.strip()
    if not name:
        raise ValueError("Directory name cannot be empty")
    if not parent_path.endswith("/"):
        parent_path += "/"
    path = parent_path + name
    existing_dirs = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .where("path", "==", path) \
        .limit(1) \
        .get()
    if len(existing_dirs) > 0:
        raise ValueError(f"Directory '{name}' already exists in '{parent_path}'")
    dir_data = {
        "name": name,
        "path": path,
        "parent_path": parent_path,
        "user_id": uid,
        "created_at": datetime.datetime.utcnow().isoformat()
    }
    doc_ref = firestore_db.collection("Directories").add(dir_data)
    return doc_ref

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
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
    uid = user_token.get("uid")
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
async def create_directory_route(request: Request, dirname: str = Form(...), parent_path: str = Form("/")):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    uid = user_token.get("uid")
    try:
        create_directory(uid, dirname, parent_path=parent_path)
    except ValueError as err:
        print(err)
    if parent_path != "/":
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", parent_path) \
            .limit(1) \
            .get()
        if parent_query:
            parent_id = parent_query[0].id
            return RedirectResponse(url=f"/directory/{parent_id}", status_code=status.HTTP_302_FOUND)
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

@app.get("/directory/{directory_id}", response_class=HTMLResponse)
async def change_directory(request: Request, directory_id: str):
    id_token_cookie = request.cookies.get("token")
    error_message = None
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    uid = user_token.get("uid")
    dir_ref = firestore_db.collection("Directories").document(directory_id)
    dir_doc = dir_ref.get()
    if not dir_doc.exists:
        error_message = "Directory not found."
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    current_dir = dir_doc.to_dict()
    if current_dir.get("user_id") != uid:
        error_message = "Unauthorized access."
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    normalized_path = current_dir["path"]
    if not normalized_path.endswith("/"):
        normalized_path += "/"
    child_query = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .where("parent_path", "==", normalized_path) \
        .stream()
    child_dirs = []
    for child in child_query:
        c_data = child.to_dict()
        c_data["id"] = child.id
        child_dirs.append(c_data)
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=normalized_path)
    files = []
    for blob in blobs:
        relative_name = blob.name[len(normalized_path):]
        if "/" in relative_name or relative_name == "":
            continue
        files.append({"name": relative_name, "full_path": blob.name})
    parent_directory = None
    if current_dir["parent_path"] != "/":
        parent_path_norm = current_dir["parent_path"]
        if parent_path_norm.endswith("/"):
            parent_path_norm = parent_path_norm[:-1]
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", parent_path_norm) \
            .limit(1) \
            .get()
        if parent_query:
            parent_directory = parent_query[0].id
    return templates.TemplateResponse("directory.html", {
        "request": request,
        "user_token": user_token,
        "error_message": error_message,
        "current_dir": current_dir,
        "child_dirs": child_dirs,
        "files": files,
        "parent_directory": parent_directory
    })

@app.post("/delete-file", response_class=RedirectResponse)
async def delete_file(request: Request, filename: str = Form(...), parent_path: str = Form(...)):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    if not parent_path.endswith("/"):
        parent_path += "/"
    blob_name = parent_path + filename
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blob = bucket.get_blob(blob_name)
    if blob is not None:
        blob.delete()
    if parent_path == "/":
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    else:
        uid = user_token.get("uid")
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", parent_path) \
            .limit(1) \
            .get()
        if parent_query:
            parent_id = parent_query[0].id
            return RedirectResponse(url=f"/directory/{parent_id}", status_code=status.HTTP_302_FOUND)
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

@app.get("/download-file", response_class=StreamingResponse)
async def download_file(request: Request, filename: str, parent_path: str):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    if not parent_path.endswith("/"):
        parent_path += "/"
    blob_name = parent_path + filename
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blob = bucket.get_blob(blob_name)
    if not blob:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    file_data = blob.download_as_bytes()
    file_stream = BytesIO(file_data)
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(file_stream, media_type=blob.content_type, headers=headers)

@app.post("/upload-file", response_class=HTMLResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    parent_path: str = Form(...),
    overwrite: str = Form(None)
):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    if not parent_path.endswith("/"):
        parent_path += "/"
    blob_name = parent_path + file.filename
    overwrite_flag = (overwrite == "true")
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blob = bucket.get_blob(blob_name)
    if blob is not None and not overwrite_flag:
        uid = user_token.get("uid")
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", parent_path) \
            .limit(1) \
            .get()
        current_dir = {}
        if parent_query:
            current_dir = parent_query[0].to_dict()
            current_dir["id"] = parent_query[0].id
        child_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("parent_path", "==", parent_path) \
            .stream()
        child_dirs = []
        for child in child_query:
            c_data = child.to_dict()
            c_data["id"] = child.id
            child_dirs.append(c_data)
        return templates.TemplateResponse("directory.html", {
            "request": request,
            "user_token": user_token,
            "error_message": f"File '{file.filename}' already exists in '{parent_path}'. Check 'Overwrite' to replace it.",
            "current_dir": current_dir,
            "child_dirs": child_dirs,
            "files": [],
            "parent_directory": None
        })
    new_blob = bucket.blob(blob_name)
    new_blob.upload_from_file(file.file, content_type=file.content_type)
    if parent_path == "/":
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    else:
        uid = user_token.get("uid")
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", parent_path) \
            .limit(1) \
            .get()
        if parent_query:
            parent_id = parent_query[0].id
            return RedirectResponse(url=f"/directory/{parent_id}", status_code=status.HTTP_302_FOUND)
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
