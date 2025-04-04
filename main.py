import re
import hashlib
import time
import local_constants  # Must define BUCKET_NAME
from fastapi import FastAPI, Request, Cookie, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import google.auth.transport.requests
import google.oauth2.id_token
from google.cloud import firestore, storage

app = FastAPI()

# Mount static files and configure templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Initialize Firestore client
db = firestore.Client()

def verify_firebase_token(id_token: str):
    """Verifies the Firebase ID token using google-auth."""
    adapter = google.auth.transport.requests.Request()
    try:
        return google.oauth2.id_token.verify_firebase_token(id_token, adapter)
    except Exception as e:
        print(f"Token verification error: {e}")
        return None

def get_user(decoded_token: dict):
    """
    Ensures a user document exists (creating one if needed along with a default root directory)
    and returns the user data with uid included.
    """
    user_id = decoded_token.get("uid") or decoded_token.get("sub")
    if not user_id:
        raise ValueError("No valid user id found in token.")
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    if not user_doc.exists:
        user_data = {
            "email": decoded_token.get("email"),
            "created_at": firestore.SERVER_TIMESTAMP,
            "uid": user_id
        }
        user_ref.set(user_data)
        print(f"User document created for {user_id}")
        # Create default root directory.
        root_dir = {
            "user_id": user_id,
            "path": "/",
            "name": "root",
            "parent_path": "/",
            "created_at": firestore.SERVER_TIMESTAMP
        }
        db.collection("directories").add(root_dir)
        print(f"Default root directory created for {user_id}")
        return user_data
    else:
        user_data = user_doc.to_dict()
        user_data["uid"] = user_id
        return user_data

def query_files(user_id: str, directory_path: str) -> list:
    """Returns files in a given directory for the user."""
    files = []
    for doc in db.collection("files")\
                 .where("user_id", "==", user_id)\
                 .where("directory_path", "==", directory_path)\
                 .stream():
        f = doc.to_dict()
        f["id"] = doc.id
        files.append(f)
    return files

def query_all_files(user_id: str) -> list:
    """Returns all files for the user."""
    files = []
    for doc in db.collection("files").where("user_id", "==", user_id).stream():
        f = doc.to_dict()
        f["id"] = doc.id
        files.append(f)
    return files

def mark_duplicate_files(files: list) -> list:
    """Marks each file with a 'duplicate' flag if its hash appears more than once."""
    hash_counts = {}
    for file in files:
        h = file.get("hash")
        if h:
            hash_counts[h] = hash_counts.get(h, 0) + 1
    for file in files:
        file["duplicate"] = hash_counts.get(file.get("hash"), 0) > 1
    return files

def group_duplicate_files(files: list) -> dict:
    """
    Groups files by their hash. For each group with more than one file,
    extracts a base filename (removing any duplicate suffix) as the title.
    Returns a dict mapping hash to {title: base_filename, files: [...] }.
    """
    groups = {}
    for f in files:
        h = f.get("hash")
        if h:
            groups.setdefault(h, []).append(f)
    duplicate_groups = {}
    for h, group in groups.items():
        if len(group) > 1:
            original = group[0].get("name", "Unknown")
            match = re.match(r'^(.*?)(?:_\d+)?(\.[^.]+)?$', original)
            base_title = match.group(1) + (match.group(2) if match.group(2) else "") if match else original
            duplicate_groups[h] = {"title": base_title, "files": group}
    return duplicate_groups

def query_shared_files(user_email: str) -> list:
    """Returns files that have been shared with the given email."""
    files = []
    for doc in db.collection("files").where("shared_with", "array_contains", user_email).stream():
        f = doc.to_dict()
        f["id"] = doc.id
        files.append(f)
    return files

# -------------------- Endpoints -------------------- #

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, token: str = Cookie(default=""), message: str = ""):
    """
    Root route: Sets current directory to root ("/") and queries for subdirectories,
    files, duplicate groups (across entire dropbox), and shared files.
    """
    user = None
    directories = []
    current_directory = {"id": None, "name": "root", "path": "/", "parent_path": "/"}
    files = []
    duplicate_groups_all = {}
    shared_files = []
    if token:
        decoded = verify_firebase_token(token)
        if decoded:
            user = get_user(decoded)
            user_id = decoded.get("uid") or decoded.get("sub")
            user_email = decoded.get("email")
            # Subdirectories under root (excluding root itself)
            for doc in db.collection("directories")\
                          .where("user_id", "==", user_id)\
                          .where("parent_path", "==", "/")\
                          .stream():
                d = doc.to_dict()
                if d.get("path") != "/":
                    d["id"] = doc.id
                    directories.append(d)
            files = mark_duplicate_files(query_files(user_id, current_directory["path"]))
            all_files = mark_duplicate_files(query_all_files(user_id))
            duplicate_groups_all = group_duplicate_files(all_files)
            shared_files = query_shared_files(user_email)
    return templates.TemplateResponse("main.html", {
        "request": request,
        "user": user,
        "current_directory": current_directory,
        "directories": directories or [],
        "parent_directory": None,
        "files": files or [],
        "duplicate_groups_all": duplicate_groups_all or {},
        "shared_files": shared_files or [],
        "message": message
    })

@app.get("/directory/{dir_id}", response_class=HTMLResponse)
async def view_directory(dir_id: str, request: Request, token: str = Cookie(default=""), message: str = ""):
    """
    View a directory: Retrieves details for the specified directory, its subdirectories,
    parent directory (if any), files, duplicate groups (across dropbox), and shared files.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user = get_user(decoded)
    user_id = decoded.get("uid") or decoded.get("sub")
    user_email = decoded.get("email")
    
    dir_ref = db.collection("directories").document(dir_id)
    dir_doc = dir_ref.get()
    if not dir_doc.exists:
        return RedirectResponse(url="/", status_code=302)
    current_directory = dir_doc.to_dict()
    if current_directory.get("user_id") != user_id:
        return RedirectResponse(url="/", status_code=302)
    current_directory["id"] = dir_doc.id

    directories = []
    for doc in db.collection("directories")\
                  .where("user_id", "==", user_id)\
                  .where("parent_path", "==", current_directory["path"])\
                  .stream():
        d = doc.to_dict()
        if d.get("path") != current_directory["path"]:
            d["id"] = doc.id
            directories.append(d)
    
    parent_directory = None
    if current_directory["path"] != "/":
        for doc in db.collection("directories")\
                     .where("user_id", "==", user_id)\
                     .where("path", "==", current_directory["parent_path"])\
                     .limit(1)\
                     .stream():
            parent_directory = doc.to_dict()
            parent_directory["id"] = doc.id
            break

    files = mark_duplicate_files(query_files(user_id, current_directory["path"]))
    all_files = mark_duplicate_files(query_all_files(user_id))
    duplicate_groups_all = group_duplicate_files(all_files)
    shared_files = query_shared_files(user_email)

    return templates.TemplateResponse("main.html", {
        "request": request,
        "user": user,
        "current_directory": current_directory,
        "directories": directories,
        "parent_directory": parent_directory,
        "files": files,
        "duplicate_groups_all": duplicate_groups_all,
        "shared_files": shared_files,
        "message": message
    })

@app.get("/logout")
async def logout():
    """Clears the token cookie and redirects to root."""
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("token")
    return response

@app.post("/create-directory")
async def create_directory(request: Request, token: str = Cookie(default="")):
    """
    Creates a new directory under the current directory.
    Prevents duplicate directories in the same location.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user = get_user(decoded)
    user_id = decoded.get("uid") or decoded.get("sub")
    form = await request.form()
    dir_name = form.get("directory_name")
    parent_path = form.get("parent_path", "/")
    current_directory_id = form.get("current_directory_id", "")
    if not dir_name:
        return RedirectResponse(url="/", status_code=302)
    # Check for duplicate directory name in the same parent.
    existing = list(db.collection("directories")
                    .where("user_id", "==", user_id)
                    .where("parent_path", "==", parent_path)
                    .where("name", "==", dir_name)
                    .stream())
    if existing:
        print("Directory already exists.")
        return RedirectResponse(url=f"/directory/{current_directory_id}?message=Directory+already+exists", status_code=302)
    new_path = "/" + dir_name if parent_path == "/" else parent_path.rstrip("/") + "/" + dir_name
    directory_data = {
        "user_id": user_id,
        "name": dir_name,
        "path": new_path,
        "parent_path": parent_path,
        "created_at": firestore.SERVER_TIMESTAMP
    }
    db.collection("directories").add(directory_data)
    print(f"Directory '{dir_name}' created at '{new_path}' for user {user_id}")
    redirect_url = f"/directory/{current_directory_id}" if current_directory_id else "/"
    return RedirectResponse(url=redirect_url, status_code=302)

@app.post("/delete-directory")
async def delete_directory(request: Request, token: str = Cookie(default="")):
    """
    Deletes a directory only if it is empty (no subdirectories or files) and is not the default root.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user_id = decoded.get("uid") or decoded.get("sub")
    form = await request.form()
    directory_id = form.get("directory_id")
    if not directory_id:
        return RedirectResponse(url="/", status_code=302)
    directory_ref = db.collection("directories").document(directory_id)
    dir_doc = directory_ref.get()
    if dir_doc.exists:
        directory = dir_doc.to_dict()
        if directory.get("user_id") == user_id and directory.get("path") != "/":
            subdirs = list(db.collection("directories")
                           .where("user_id", "==", user_id)
                           .where("parent_path", "==", directory.get("path"))
                           .stream())
            files = list(db.collection("files")
                         .where("user_id", "==", user_id)
                         .where("directory_path", "==", directory.get("path"))
                         .stream())
            if subdirs or files:
                print("Directory not empty; cannot delete.")
                return RedirectResponse(url=f"/directory/{directory_id}?message=Directory+not+empty", status_code=302)
            else:
                directory_ref.delete()
                print(f"Directory {directory_id} deleted for user {user_id}")
        else:
            print("Unauthorized deletion attempt or default root deletion.")
    return RedirectResponse(url="/", status_code=302)

@app.post("/upload-file")
async def upload_file(
    request: Request,
    token: str = Cookie(default=""),
    file: UploadFile = File(...),
    current_directory_path: str = Form(...),
    current_directory_id: str = Form(...),
    action: str = Form(default="")  # Expected: "overwrite" or "duplicate"
):
    """
    Uploads a file to the current directory.
    If a file with the same name exists and no action is specified, redirects with a message.
    If action=="overwrite", uploads to the same path.
    If action=="duplicate", appends a timestamp to create a unique filename.
    Saves a SHA-256 hash for duplicate detection.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user_id = decoded.get("uid") or decoded.get("sub")
    filename = file.filename
    base_path = "/" + filename if current_directory_path == "/" else current_directory_path.rstrip("/") + "/" + filename
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blob = bucket.blob(base_path)
    if blob.exists():
        if not action:
            print("File exists; no action specified.")
            return RedirectResponse(url=f"/directory/{current_directory_id}?message=File+exists,+please+select+an+action", status_code=302)
        elif action == "overwrite":
            file_path = base_path
        elif action == "duplicate":
            unique_suffix = f"_{int(time.time())}"
            if '.' in filename:
                parts = filename.rsplit('.', 1)
                new_filename = parts[0] + unique_suffix + "." + parts[1]
            else:
                new_filename = filename + unique_suffix
            file_path = "/" + new_filename if current_directory_path == "/" else current_directory_path.rstrip("/") + "/" + new_filename
            filename = new_filename
        else:
            print("Unrecognized action.")
            return RedirectResponse(url=f"/directory/{current_directory_id}?message=Please+select+overwrite+or+duplicate", status_code=302)
    else:
        file_path = base_path
    file_contents = await file.read()
    file_hash = hashlib.sha256(file_contents).hexdigest()
    blob = bucket.blob(file_path)
    blob.upload_from_string(file_contents)
    print(f"File '{filename}' uploaded to '{file_path}' for user {user_id}")
    file_data = {
        "user_id": user_id,
        "name": filename,
        "path": file_path,
        "directory_path": current_directory_path,
        "uploaded_at": firestore.SERVER_TIMESTAMP,
        "hash": file_hash,
        "sender_email": decoded.get("email")
    }
    db.collection("files").add(file_data)
    return RedirectResponse(url=f"/directory/{current_directory_id}", status_code=302)

@app.post("/delete-file")
async def delete_file(request: Request, token: str = Cookie(default="")):
    """
    Deletes a file from Cloud Storage and Firestore.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user_id = decoded.get("uid") or decoded.get("sub")
    form = await request.form()
    file_id = form.get("file_id")
    current_directory_id = form.get("current_directory_id", "")
    if not file_id:
        return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)
    file_ref = db.collection("files").document(file_id)
    file_doc = file_ref.get()
    if file_doc.exists:
        file_data = file_doc.to_dict()
        if file_data.get("user_id") == user_id:
            storage_client = storage.Client()
            bucket = storage_client.bucket(local_constants.BUCKET_NAME)
            blob = bucket.blob(file_data.get("path"))
            if blob.exists():
                blob.delete()
            file_ref.delete()
            print(f"File '{file_data.get('name')}' deleted for user {user_id}")
    return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)

@app.post("/share-file")
async def share_file(request: Request, token: str = Cookie(default="")):
    """
    Shares a file read-only with another user by updating its 'shared_with' array.
    Only the file owner can share the file.
    Expects form data: file_id, share_email, current_directory_id.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user_id = decoded.get("uid") or decoded.get("sub")
    form = await request.form()
    file_id = form.get("file_id")
    share_email = form.get("share_email")
    current_directory_id = form.get("current_directory_id", "")
    if not file_id or not share_email:
        return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)
    file_ref = db.collection("files").document(file_id)
    file_doc = file_ref.get()
    if file_doc.exists:
        file_data = file_doc.to_dict()
        if file_data.get("user_id") == user_id:
            shared_with = file_data.get("shared_with", [])
            if share_email not in shared_with:
                shared_with.append(share_email)
                file_ref.update({"shared_with": shared_with})
                print(f"File '{file_data.get('name')}' shared with {share_email}")
        else:
            print("Not file owner; cannot share.")
    return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)

@app.get("/download-file/{file_id}")
async def download_file(file_id: str, token: str = Cookie(default=""), current_directory_id: str = None):
    """
    Downloads a file if the user is the owner or if their email is in the file's shared_with list.
    """
    if not token:
        return RedirectResponse(url="/", status_code=302)
    decoded = verify_firebase_token(token)
    if not decoded:
        return RedirectResponse(url="/", status_code=302)
    user_id = decoded.get("uid") or decoded.get("sub")
    user_email = decoded.get("email")
    file_ref = db.collection("files").document(file_id)
    file_doc = file_ref.get()
    if not file_doc.exists:
        return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)
    file_data = file_doc.to_dict()
    if file_data.get("user_id") != user_id and user_email not in file_data.get("shared_with", []):
        return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)
    storage_client = storage.Client()
    bucket = storage_client.bucket(local_constants.BUCKET_NAME)
    blob = bucket.blob(file_data.get("path"))
    if not blob.exists():
        return RedirectResponse(url=f"/directory/{current_directory_id}" if current_directory_id else "/", status_code=302)
    file_bytes = blob.download_as_bytes()
    return Response(content=file_bytes,
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={file_data.get('name')}"})
