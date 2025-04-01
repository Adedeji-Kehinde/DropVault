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
            "directories": [],
            "duplicate_files": {}
        })
    uid = user_token.get("uid")
    
    # Query root-level directories
    dirs_query = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .where("parent_path", "==", "/") \
        .stream()
    directories = []
    for d in dirs_query:
        dir_data = d.to_dict()
        dir_data["id"] = d.id
        directories.append(dir_data)
    
    # Gather all directory prefixes for this user (including root)
    dirs_query_all = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .stream()
    prefixes = set()
    for d in dirs_query_all:
        data = d.to_dict()
        path = data.get("path", "")
        if path:
            if not path.endswith("/"):
                path += "/"
            prefixes.add(path)
    prefixes.add("/")  # Include root
    
    # Collect all files from Cloud Storage for each prefix
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    all_files = []
    for prefix in prefixes:
        blobs = bucket.list_blobs(prefix=prefix)
        for blob in blobs:
            blob.reload()  # Refresh blob metadata
            relative_name = blob.name[len(prefix):]
            # Skip if file is not directly under this prefix
            if not relative_name or "/" in relative_name:
                continue
            file_info = {
                "name": relative_name,
                "full_path": blob.name,
                "md5": blob.md5_hash
            }
            all_files.append(file_info)
    
    # Build a dictionary mapping MD5 hash to list of files
    duplicates_dict = {}
    for f in all_files:
        md5 = f.get("md5")
        if not md5:
            continue
        duplicates_dict.setdefault(md5, []).append(f)
    
    # Filter out non-duplicates (only keep entries with more than one file)
    duplicate_files = {md5: files for md5, files in duplicates_dict.items() if len(files) > 1}
    
    return templates.TemplateResponse("main.html", {
        "request": request,
        "user_token": user_token,
        "error_message": error_message,
        "directories": directories,
        "duplicate_files": duplicate_files
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

@app.post("/delete-directory", response_class=HTMLResponse)
async def delete_directory_route(request: Request, directory_id: str = Form(...)):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    uid = user_token.get("uid")
    dir_ref = firestore_db.collection("Directories").document(directory_id)
    dir_doc = dir_ref.get()
    if not dir_doc.exists:
        error_message = "Directory not found."
        return templates.TemplateResponse("main.html", {
            "request": request,
            "user_token": user_token,
            "error_message": error_message,
            "directories": []
        })
    
    dir_data = dir_doc.to_dict()
    if dir_data.get("user_id") != uid:
        error_message = "Unauthorized deletion attempt."
        return templates.TemplateResponse("main.html", {
            "request": request,
            "user_token": user_token,
            "error_message": error_message,
            "directories": []
        })
    
    # Normalize the directory path (ensure it ends with "/")
    normalized_path = dir_data["path"]
    if not normalized_path.endswith("/"):
        normalized_path += "/"
    
    # Check for child directories under this directory
    child_dirs = list(firestore_db.collection("Directories")
                      .where("user_id", "==", uid)
                      .where("parent_path", "==", normalized_path)
                      .stream())
    
    # Check for files in Cloud Storage for this directory
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blobs = list(bucket.list_blobs(prefix=normalized_path))
    # Filter blobs to list files directly under the current directory
    files = []
    for blob in blobs:
        relative_name = blob.name[len(normalized_path):]
        if "/" in relative_name or relative_name == "":
            continue
        files.append(blob)
    
    if child_dirs or files:
        error_message = "Directory is not empty. Please delete its subdirectories and files first."
        # Determine parent's document ID (for navigation)
        parent_directory = None
        if dir_data["parent_path"] != "/":
            parent_path_norm = dir_data["parent_path"]
            if parent_path_norm.endswith("/"):
                parent_path_norm = parent_path_norm[:-1]
            parent_query = firestore_db.collection("Directories") \
                .where("user_id", "==", uid) \
                .where("path", "==", parent_path_norm) \
                .limit(1) \
                .get()
            if parent_query:
                parent_directory = parent_query[0].id
        
        # Query child directories again for display
        child_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("parent_path", "==", normalized_path) \
            .stream()
        child_list = []
        for child in child_query:
            c_data = child.to_dict()
            c_data["id"] = child.id
            child_list.append(c_data)
        # Prepare file list for display
        files_list = []
        for blob in blobs:
            relative_name = blob.name[len(normalized_path):]
            if "/" in relative_name or relative_name == "":
                continue
            files_list.append({"name": relative_name, "full_path": blob.name})
        
        return templates.TemplateResponse("directory.html", {
            "request": request,
            "user_token": user_token,
            "error_message": error_message,
            "current_dir": dir_data,
            "child_dirs": child_list,
            "files": files_list,
            "parent_directory": parent_directory
        })
    
    # If the directory is empty, delete it
    dir_ref.delete()
    # Redirect: if the deleted directory is under root, return to main; otherwise, return to its parent directory view
    if dir_data["parent_path"] == "/":
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    else:
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", dir_data["parent_path"]) \
            .limit(1) \
            .get()
        if parent_query:
            parent_id = parent_query[0].id
            return RedirectResponse(url=f"/directory/{parent_id}", status_code=status.HTTP_302_FOUND)
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

@app.get("/directory/{directory_id}", response_class=HTMLResponse)
async def change_directory(request: Request, directory_id: str):
    id_token_cookie = request.cookies.get("token")
    error_message = None
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    uid = user_token.get("uid")
    # Retrieve the current directory document
    dir_ref = firestore_db.collection("Directories").document(directory_id)
    dir_doc = dir_ref.get()
    if not dir_doc.exists:
        error_message = "Directory not found."
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    current_dir = dir_doc.to_dict()
    if current_dir.get("user_id") != uid:
        error_message = "Unauthorized access."
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    # Normalize current directory path for querying child directories and files
    normalized_path = current_dir["path"]
    if not normalized_path.endswith("/"):
        normalized_path += "/"
    # Query for child directories
    child_query = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .where("parent_path", "==", normalized_path) \
        .stream()
    child_dirs = []
    for child in child_query:
        c_data = child.to_dict()
        c_data["id"] = child.id
        child_dirs.append(c_data)
    # List files from Cloud Storage in the current directory
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=normalized_path)
    files = []
    for blob in blobs:
        relative_name = blob.name[len(normalized_path):]
        if "/" in relative_name or relative_name == "":
            continue
        files.append({
            "name": relative_name,
            "full_path": blob.name,
            "md5": blob.md5_hash
        })
    # Detect duplicates within the current directory
    md5_counts = {}
    for f in files:
        h = f.get("md5")
        if h:
            md5_counts[h] = md5_counts.get(h, 0) + 1
    duplicate_files_current = {md5: [f for f in files if f.get("md5") == md5] 
                               for md5 in md5_counts if md5_counts[md5] > 1}
    # Determine parent's document ID for "../" navigation if not root
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
        "duplicate_files_current": duplicate_files_current,
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
    action: str = Form(None)  # Now action may be None if user didn't select anything
):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    if not parent_path.endswith("/"):
        parent_path += "/"
    original_blob_name = parent_path + file.filename
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blob = bucket.get_blob(original_blob_name)
    
    # If a blob exists and no action is provided, ask user to select one
    if blob is not None and not action:
        uid = user_token.get("uid")
        # Query current directory details
        parent_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("path", "==", parent_path) \
            .limit(1) \
            .get()
        current_dir = {}
        if parent_query:
            current_dir = parent_query[0].to_dict()
            current_dir["id"] = parent_query[0].id
        else:
            trimmed = parent_path.rstrip("/")
            fallback_name = trimmed.split("/")[-1] if trimmed else "/"
            current_dir = {"path": parent_path, "name": fallback_name, "parent_path": "/"}

        # Retrieve subdirectories for display
        child_query = firestore_db.collection("Directories") \
            .where("user_id", "==", uid) \
            .where("parent_path", "==", parent_path) \
            .stream()
        child_dirs = []
        for child in child_query:
            c_data = child.to_dict()
            c_data["id"] = child.id
            child_dirs.append(c_data)
        # Retrieve files list
        blobs = bucket.list_blobs(prefix=parent_path)
        files_list = []
        for b in blobs:
            relative_name = b.name[len(parent_path):]
            if "/" in relative_name or relative_name == "":
                continue
            files_list.append({"name": relative_name, "full_path": b.name})
        
        return templates.TemplateResponse("directory.html", {
            "request": request,
            "user_token": user_token,
            "error_message": f"File '{file.filename}' already exists in '{parent_path}'. Please select an action.",
            "current_dir": current_dir,
            "child_dirs": child_dirs,
            "files": files_list,
            "parent_directory": None,
            "duplicate_prompt": True  # Optional flag for UI adjustments
        })
    
    # Process action if duplicate exists
    if blob is not None:
        if action == "overwrite":
            blob = bucket.blob(original_blob_name)
        elif action == "duplicate":
            timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            if '.' in file.filename:
                base, ext = file.filename.rsplit('.', 1)
                new_filename = f"{base}_{timestamp}.{ext}"
            else:
                new_filename = f"{file.filename}_{timestamp}"
            new_blob_name = parent_path + new_filename
            blob = bucket.blob(new_blob_name)
        else:
            # Unrecognized action—should not happen if UI is correct.
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
            return templates.TemplateResponse("directory.html", {
                "request": request,
                "user_token": user_token,
                "error_message": "Unrecognized file upload action.",
                "current_dir": current_dir,
                "child_dirs": [],
                "files": [],
                "parent_directory": None
            })
    else:
        blob = bucket.blob(original_blob_name)
    
    blob.upload_from_file(file.file, content_type=file.content_type)
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

    
@app.get("/duplicates", response_class=HTMLResponse)
async def duplicates(request: Request):
    id_token_cookie = request.cookies.get("token")
    user_token = validate_firebase_token(id_token_cookie)
    if not user_token:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    uid = user_token.get("uid")
    
    # Get all directory prefixes for this user from Firestore
    dirs_query = firestore_db.collection("Directories") \
        .where("user_id", "==", uid) \
        .stream()
    prefixes = set()
    for d in dirs_query:
        data = d.to_dict()
        path = data.get("path", "")
        if path:
            if not path.endswith("/"):
                path += "/"
            prefixes.add(path)
    # Also include root explicitly
    prefixes.add("/")
    
    # Collect all files from Cloud Storage for these prefixes
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    all_files = []
    for prefix in prefixes:
        blobs = bucket.list_blobs(prefix=prefix)
        for blob in blobs:
            blob.reload()  # Force reload of the blob properties
            # Extract relative file name from the prefix
            relative_name = blob.name[len(prefix):]
            if not relative_name or "/" in relative_name:
                continue
            file_info = {
                "name": relative_name,
                "full_path": blob.name,
                "md5": blob.md5_hash
            }
            all_files.append(file_info)
    
    # Build a dictionary mapping MD5 hash to list of files
    duplicates_dict = {}
    for f in all_files:
        md5 = f.get("md5")
        if not md5:
            continue
        duplicates_dict.setdefault(md5, []).append(f)
    
    # Filter to only duplicate entries (more than one file with the same MD5)
    duplicate_files = {md5: files for md5, files in duplicates_dict.items() if len(files) > 1}
    
    # Render your duplicates view (or integrate into your main view)
    return templates.TemplateResponse("duplicates.html", {
        "request": request,
        "user_token": user_token,
        "duplicate_files": duplicate_files
    })


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
