from fastapi import FastAPI, Form, Request, Depends, HTTPException, status, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlencode, urljoin
from typing import Optional
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from azure.storage.blob import BlobServiceClient
import csv
import json
import io
import os
from dotenv import load_dotenv
load_dotenv()

QUIZZES_DIRECTORY = os.getenv("QUIZZES_DIRECTORY")

# Ensure the quizzes directory exists
app = FastAPI()

# Serve static files (CSS, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Set up Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Azure Blob Storage configuration
AZURE_CONNECTION_STRING = os.getenv("AZURE_CONNECTION_STRING")
BLOB_CONTAINER_NAME = os.getenv("BLOB_CONTAINER_NAME")
BLOB_NAME = os.getenv("BLOB_NAME")

# Initialize Azure Blob Storage client
blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

# Ensure container exists
try:
    container_client.create_container()
except Exception:
    pass

# Initialize quiz and result storage
quizzes = {}
interview_results = []

# Security dependency
security = HTTPBasic()

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "secret"
def enumerated(item):
    return list(enumerate(item))

templates.env.globals['enumerated'] = enumerated
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = credentials.username == ADMIN_USERNAME
    correct_password = credentials.password == ADMIN_PASSWORD
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

@app.get("/", response_class=HTMLResponse)
async def welcome(request: Request):
    quiz_name = request.query_params.get("quiz_name", "") 
    return templates.TemplateResponse("welcome.html", {"request": request, "quiz_name": quiz_name})

@app.post("/start-quiz")
async def start_quiz(request: Request, quiz_name: str = Form(...), name: str = Form(...), email: str = Form(...)):
    print("Quiz name is:", quiz_name)
    # Load the quiz from Azure Blob Storage
    blob_client = container_client.get_blob_client(f"{QUIZZES_DIRECTORY}/{quiz_name}.json")
    
    try:
        blob_data = blob_client.download_blob().readall()
        quiz_data = json.loads(blob_data.decode('utf-8'))
    except:
        raise HTTPException(status_code=404, detail="Quiz not found")

    # Render the index.html with the quiz data and user details
    return templates.TemplateResponse("index.html", {
        "request": request,
        "quiz_name": quiz_name,
        "name": name,
        "email": email,
        "questions": quiz_data.get("questions", [])
    })

@app.post("/submit")
async def submit(request: Request, quiz_name: str = Form(...), name: str = Form(...), email: str = Form(...)):
    form_data = await request.form()
    
    # Extract the user answers (fields starting with "q" followed by a digit)
    user_answers = {key: value for key, value in form_data.items() if key.startswith('q') and key[1:].isdigit()}
    
    # Load the quiz data from Azure Blob Storage
    blob_client = container_client.get_blob_client(f"{QUIZZES_DIRECTORY}/{quiz_name}.json")
    
    try:
        blob_data = blob_client.download_blob().readall()
        quiz_data = json.loads(blob_data.decode('utf-8'))
    except json.JSONDecodeError:
        return HTMLResponse("<h1>Error: Failed to load quiz data!</h1>", status_code=500)

    questions = quiz_data.get("questions", [])
    
    # Calculate the score, ensuring only valid questions are counted
    try:
        score = sum(1 for i, answer in user_answers.items() if questions[int(i[1:])]["answer"] == answer)
    except (KeyError, IndexError, ValueError) as e:
        return HTMLResponse(f"<h1>Error: Invalid form submission!</h1><p>{str(e)}</p>", status_code=400)

    result = {"name": name, "email": email, "Role":quiz_name, "score": score}

    # Append the result to the existing blob data
    blob_client = container_client.get_blob_client(BLOB_NAME)
    try:
        blob_data = blob_client.download_blob().readall()
        existing_csv = io.StringIO(blob_data.decode('utf-8'))
        existing_csv_reader = csv.DictReader(existing_csv)
        existing_results = list(existing_csv_reader)
    except Exception as e:
        # If the blob doesn't exist or can't be downloaded, start with an empty list
        existing_results = []

    # Add the new result to the existing results
    existing_results.append(result)
    
    # Write the updated results back to the blob
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["name", "email", "Role","score"])
    writer.writeheader()
    writer.writerows(existing_results)
    
    try:
        blob_client.upload_blob(output.getvalue(), overwrite=True)
    except Exception as e:
        return HTMLResponse(f"<h1>Error: Failed to save results!</h1><p>{str(e)}</p>", status_code=500)
    
    return templates.TemplateResponse("thankyoupage.html", {"name": name,"request":request})
@app.get("/admin", response_class=HTMLResponse)
async def admin(
    request: Request, 
    authorized: bool = Depends(authenticate), 
    view: str = "select-quiz", 
    quiz_name: str = Query(None), 
    qualifying_score: Optional[int] = Query(None)
):
    quizzes = []
    quiz_link = None

    if view == "select-quiz":
        # Read quizzes from Azure Blob Storage
        blob_list = container_client.list_blobs(name_starts_with=QUIZZES_DIRECTORY)
        quizzes = [blob.name.split('/')[-1].replace('.json', '') for blob in blob_list if blob.name.endswith('.json')]

        # If a quiz is selected, generate the link
        if quiz_name:
            query_params = urlencode({"quiz_name": quiz_name})
            quiz_link = urljoin(str(request.url_for('welcome')), f"?{query_params}")
    
    shortlisted_csv = False

    if view == 'results' and qualifying_score is not None:
        # Read all results from Azure Blob Storage
        blob_client = container_client.get_blob_client(BLOB_NAME)
        blob_data = blob_client.download_blob().readall()
        existing_csv = io.StringIO(blob_data.decode('utf-8'))
        existing_csv_reader = csv.DictReader(existing_csv)
        existing_results = list(existing_csv_reader)

        # Filter results based on qualifying score
        shortlisted_candidates = [result for result in existing_results if int(result['score']) >= qualifying_score]

        # Generate a new CSV for shortlisted candidates
        shortlisted_output = io.StringIO()
        writer = csv.DictWriter(shortlisted_output, fieldnames=["name", "email", "Role", "score"])
        writer.writeheader()
        writer.writerows(shortlisted_candidates)

        # Upload the shortlisted candidates CSV to Azure Blob Storage
        blob_client = container_client.get_blob_client("shortlisted_candidates.csv")
        blob_client.upload_blob(shortlisted_output.getvalue(), overwrite=True)
        
        shortlisted_csv = True

    # Render the admin template with the filtered results
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "view": view,
        "quiz_name": quiz_name,
        "quiz_link": quiz_link,
        "quizzes": quizzes,
        "shortlisted_csv": shortlisted_csv,
        "qualifying_score": qualifying_score  # Pass the score back to the template only for the results view
    })
@app.post("/add-question")
async def add_question(
    request: Request,
    quiz_name: str = Form(...),
    question: str = Form(...),
    option1: str = Form(...),
    option2: str = Form(...),
    option3: str = Form(...),
    option4: str = Form(...),
    answer: str = Form(...)):
    
    if quiz_name not in quizzes:
        quizzes[quiz_name] = []

    quizzes[quiz_name].append({
        "question": question,
        "options": [option1, option2, option3, option4],
        "answer": answer
    })

    return RedirectResponse(url=f"/admin?view=make-quiz&quiz_name={quiz_name}", status_code=303)

# Define a Pydantic model to handle the incoming questions
class Question(BaseModel):
    question: str
    option1: str
    option2: str
    option3: str
    option4: str
    answer: str

@app.get("/download-results")
async def download_results():
    blob_client = container_client.get_blob_client(BLOB_NAME)
    blob_data = blob_client.download_blob()
    csv_content = blob_data.readall()

    return HTMLResponse(content=csv_content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=interview_results.csv"})

@app.post("/create-quiz")
async def create_quiz(request: Request, quiz_name: str = Form(...)):
    if quiz_name not in quizzes:
        quizzes[quiz_name] = []
    return RedirectResponse(url=f"/admin?view=make-quiz&quiz_name={quiz_name}", status_code=303)

@app.post("/save-quiz")
async def save_quiz(request: Request, quiz_name: str = Form(...)):
    form = await request.form()

    # Initialize an empty list to store the questions
    structured_questions = []

    # Iterate through form fields and group them into questions
    question_index = 0
    while True:
        question_text = form.get(f"questions[{question_index}][question]")
        if not question_text:  # Stop if no more questions
            break
        
        options = [
            form.get(f"questions[{question_index}][option1]"),
            form.get(f"questions[{question_index}][option2]"),
            form.get(f"questions[{question_index}][option3]"),
            form.get(f"questions[{question_index}][option4]")
        ]
        answer = form.get(f"questions[{question_index}][answer]")

        structured_questions.append({
            "question": question_text,
            "options": options,
            "answer": answer
        })

        question_index += 1

    # Prepare the quiz data
    quiz_data = {
        "quiz_name": quiz_name,
        "questions": structured_questions
    }

    # Convert quiz data to JSON
    quiz_json = json.dumps(quiz_data, indent=4)

    # Save quiz data to Azure Blob Storage
    blob_client = container_client.get_blob_client(f"{QUIZZES_DIRECTORY}/{quiz_name}.json")
    try:
        blob_client.upload_blob(quiz_json, overwrite=True)
    except Exception as e:
        return HTMLResponse(f"<h1>Error: Failed to save quiz data!</h1><p>{str(e)}</p>", status_code=500)

    return RedirectResponse(url="/admin?view=select-quiz", status_code=303)

@app.get("/load-quiz")
async def load_quiz(quiz_name: str = Query(...)):
    try: 
        quiz_file_path = os.path.join(QUIZZES_DIRECTORY, f"{quiz_name}.json")

        if os.path.exists(quiz_file_path):
            with open(quiz_file_path, "r") as f:
                quiz_data = json.load(f)
            return JSONResponse(content=quiz_data)
        else:
            return JSONResponse(content={"questions": []}, status_code=404)
    except ValueError as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
@app.get("/download-shortlisted-results")
async def download_shortlisted_results():
    blob_client = container_client.get_blob_client("shortlisted_candidates.csv")
    blob_data = blob_client.download_blob()
    csv_content = blob_data.readall()

    return HTMLResponse(content=csv_content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=shortlisted_candidates.csv"})
@app.get("/filter-candidates")
async def filter_candidates(qualifying_score: int):
    # Read all results from Azure Blob Storage
    blob_client = container_client.get_blob_client(BLOB_NAME)
    blob_data = blob_client.download_blob().readall()
    existing_csv = io.StringIO(blob_data.decode('utf-8'))
    existing_csv_reader = csv.DictReader(existing_csv)
    existing_results = list(existing_csv_reader)

    # Filter results based on the qualifying score
    shortlisted_candidates = [result for result in existing_results if int(result['score']) >= qualifying_score]

    # Generate a new CSV for shortlisted candidates
    shortlisted_output = io.StringIO()
    writer = csv.DictWriter(shortlisted_output, fieldnames=["name", "email", "Role", "score"])
    writer.writeheader()
    writer.writerows(shortlisted_candidates)

    # Upload the shortlisted candidates CSV to Azure Blob Storage
    shortlisted_blob_name = "shortlisted_candidates.csv"
    blob_client = container_client.get_blob_client(shortlisted_blob_name)
    blob_client.upload_blob(shortlisted_output.getvalue(), overwrite=True)

    # Generate URL for the shortlisted candidates CSV file
    shortlisted_csv_url = f"/download-shortlisted-results"

    return {"shortlisted_csv_url": shortlisted_csv_url}