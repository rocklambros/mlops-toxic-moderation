---
title: "Final Course Project: Building a Production-Grade MLOps System"
type: assignment
course_id: 223323
source_url: https://canvas.du.edu/courses/223323/assignments/2011241
due_date: 2026-08-18T23:59:00
points: 25
submission_types: [text_entry, website_url, file_upload]
team_size: pairs (recommended)
---

# Final Course Project: Building a Production-Grade MLOps System

## Summary
Design, build, and deploy a complete, end-to-end machine learning application
that applies MLOps best practices across the full lifecycle: model
experimentation and versioning, automated testing, AWS deployment, and live
monitoring. Working in pairs is recommended.

## Objective
Build a production-ready ML service. Choose one of four provided problems; the
implemented system must satisfy all technical requirements below.

## Core System Requirements
The final system is a multi-component application, fully deployed on AWS:

1. **Experiment Tracking & Model Registry** — Log experiment parameters/metrics and manage model versions.
2. **ML Model Backend** — A FastAPI application that serves the registered model.
3. **Persistent Data Store** — A cloud-native database (SQL or NoSQL) for prediction logs, user feedback, and related data.
4. **Frontend Interface** — A user-facing application for interacting with the model.
5. **Model Monitoring Dashboard** — Visualizes model performance and data drift in production.
6. **CI/CD Pipeline** — Automated workflow to test and validate code changes.

## Phase 1 — Experimentation and Model Management
- **1.1 Model Development:** Choose a dataset and train a baseline ML model.
- **1.2 Experiment Tracking:** Integrate a tool such as Weights & Biases. For each run, log code version (Git commit), hyperparameters, performance metrics (e.g., accuracy, F1-score), and data versions.
- **1.3 Model Versioning & Registry:** Save trained models as artifacts. Use the Model Registry to version models and promote the best-performing model to a "Staging" or "Production" stage.

## Phase 2 — Backend API and Database Integration
- **2.1 FastAPI Backend:** Build a robust FastAPI app that loads a specific model version (e.g., latest "Production" model) from the Model Registry and serves predictions. Implement at minimum a `/predict` endpoint and a `/health` check endpoint.
- **2.2 Cloud Database:** Set up a managed AWS database.
  - SQL option: AWS RDS (e.g., PostgreSQL).
  - NoSQL option: Amazon DynamoDB.
  - The FastAPI service must log every prediction request, its output, and a timestamp for monitoring. It may also cache predictions to avoid recomputing frequent requests (e.g., store recommendations for frequent users in DynamoDB and reuse them when present).

## Phase 3 — Frontend and Live Monitoring
- **3.1 User Interface:** Build a user-facing frontend.
  - Option A (recommended): Streamlit dashboard.
  - Option B (advanced): React-based interface.
  - The frontend sends data to the FastAPI backend and displays the model's prediction.
- **3.2 Model Monitoring Dashboard:** A separate frontend app on a different EC2 server (data exchanged via the database, not JSON files). It connects to the cloud database (RDS/DynamoDB) and visualizes:
  - Prediction latency over time.
  - Distribution of predicted classes (target drift).
  - A mechanism to collect user feedback to calculate live accuracy.

## Phase 4 — Testing and CI/CD Automation
- **4.1 Comprehensive Testing:**
  - Unit tests for individual functions (e.g., data preprocessing).
  - Integration tests for FastAPI endpoints using `pytest`.
- **4.2 CI/CD Pipeline:** Set up a GitHub Actions workflow (`.github/workflows/ci.yml`) that triggers on pull requests to `main`. It must run a linter (e.g., `flake8` or `ruff`) and the full test suite (`pytest`). PRs cannot merge if checks fail.

## Phase 5 — Containerization and Deployment
- **5.1 Docker Packaging:** Containerize components (e.g., one container for the FastAPI backend, one for the frontend).
- **5.2 AWS Deployment:** Deploy containers to separate EC2 instances with Docker installed.
- **5.3 Documentation:** Provide a high-quality `README.md` in the GitHub repo covering setup instructions, deployment steps, and example user requests.

## Project Topics (choose one)
| Topic | Problem | Dataset |
|-------|---------|---------|
| Taxi Fare / ETA Prediction | Predict trip fare or duration from pickup time/geo and trip features; real-time scoring for incoming rides. | NYC TLC Trip Records (AWS Open Data + NYC site) |
| Personalized Book Recommender | Recommend books given a user's favorite titles. | Amazon Review Data — Books subset |
| U.S. Flight Delay Prediction & Ops Dashboard | Predict arrival delays (e.g., >15 min); surface route-level reliability. | U.S. DOT/BTS On-Time Performance |
| Toxic Comment Moderation | Classify comments into toxicity categories; expose a moderation endpoint with human-review workflow. | Jigsaw Toxic Comment Classification (English) |

## Deliverables & Submission
- **GitHub Repository URL:** Public repo with all code, configuration files, and documentation.
- **Project Workflow Screenshots:** AWS Console and the working prototype running live on EC2.
- **Experiment Tracking Dashboard URL:** Public W&B project dashboard.

Submit the above as the final project.