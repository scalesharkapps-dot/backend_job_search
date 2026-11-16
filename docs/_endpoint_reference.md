_Generated from OpenAPI — 95 operations. `*` = required. Bodies list the accepted fields; see `/docs` for types and limits._

### Admin

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/admin/audit-logs` | Admin | `action`, `actor_id`, `target_type`, `target_id`, `page`, `page_size` |  | Administrative audit log |
| GET | `/api/v1/admin/dashboard` | Admin |  |  | Platform statistics |
| GET | `/api/v1/admin/employers` | Admin | `verification_status`, `account_status`, `q`, `page`, `page_size` |  | List employers |
| GET | `/api/v1/admin/employers/{employer_id}` | Admin | `employer_id`* |  | Employer details |
| PATCH | `/api/v1/admin/employers/{employer_id}/status` | Admin | `employer_id`* | JSON: `reason`*, `account_status`* | Suspend or reactivate an employer |
| PATCH | `/api/v1/admin/employers/{employer_id}/verification` | Admin | `employer_id`* | JSON: `decision`*, `reason` | Review a verification request |
| GET | `/api/v1/admin/jobs` | Admin | `status`, `source`, `employer_id`, `q`, `page`, `page_size` |  | List jobs for moderation |
| GET | `/api/v1/admin/jobs/{job_id}` | Admin | `job_id`* |  | Job details (any status) |
| PATCH | `/api/v1/admin/jobs/{job_id}/moderation` | Admin | `job_id`* | JSON: `decision`*, `reason` | Approve or reject a pending job |
| PATCH | `/api/v1/admin/jobs/{job_id}/status` | Admin | `job_id`* | JSON: `reason`*, `status`* | Suspend, close or archive a job |
| GET | `/api/v1/admin/reports` | Admin | `status`, `target_type`, `page`, `page_size` |  | List abuse reports |
| GET | `/api/v1/admin/reports/{report_id}` | Admin | `report_id`* |  | Report details |
| PATCH | `/api/v1/admin/reports/{report_id}/status` | Admin | `report_id`* | JSON: `status`*, `resolution_note` | Update a report |
| GET | `/api/v1/admin/users` | Admin | `q`, `role`, `is_active`, `page`, `page_size` |  | List users |
| GET | `/api/v1/admin/users/{user_id}` | Admin | `user_id`* |  | User details |
| PATCH | `/api/v1/admin/users/{user_id}/role` | Admin | `user_id`* | JSON: `reason`*, `role`* | Change a user's role |
| PATCH | `/api/v1/admin/users/{user_id}/status` | Admin | `user_id`* | JSON: `reason`*, `is_active`* | Suspend or reactivate a user |

### Applications

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/applications` | Authenticated | `status`, `company`, `title`, `location`, `source`, `employment_type`, `applied_from`, `applied_to`, `deadline_before`, `follow_up_before`, `interview_from`, `interview_to`, `sort`, `page`, `page_size` |  | List my applications |
| POST | `/api/v1/applications` | Authenticated |  | JSON: `job_id`, `job_title`, `company_name`, `job_location`, `job_apply_url`, `employment_type`, `salary_information`, `status`, `applied_at`, `deadline`, `follow_up_date`, `reminder_text`, `notes`, `selected_cv_id`, `discovered_via`, `cover_letter` | Create an application record |
| GET | `/api/v1/applications/reminders` | Authenticated | `days` |  | Upcoming deadlines, follow-ups and interviews |
| GET | `/api/v1/applications/statistics` | Authenticated |  |  | My application statistics |
| DELETE | `/api/v1/applications/{application_id}` | Authenticated | `application_id`* |  | Delete an application record |
| GET | `/api/v1/applications/{application_id}` | Authenticated | `application_id`* |  | Application details |
| PUT | `/api/v1/applications/{application_id}` | Authenticated | `application_id`* | JSON: `job_title`, `company_name`, `job_location`, `job_apply_url`, `employment_type`, `salary_information`, `applied_at`, `deadline`, `follow_up_date`, `reminder_text`, `notes`, `selected_cv_id` | Update application details |
| GET | `/api/v1/applications/{application_id}/history` | Authenticated | `application_id`* |  | Status history |
| GET | `/api/v1/applications/{application_id}/interviews` | Authenticated | `application_id`* |  | List interviews |
| POST | `/api/v1/applications/{application_id}/interviews` | Authenticated | `application_id`* | JSON: `interview_type`*, `interview_round`, `scheduled_at`*, `timezone`, `duration_minutes`, `location_or_meeting_link`, `interviewer_name`, `notes`, `outcome`, `update_application_status` | Record an interview |
| PATCH | `/api/v1/applications/{application_id}/status` | Authenticated | `application_id`* | JSON: `status`*, `notes` | Change application status |

### Authentication

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| POST | `/api/v1/auth/forgot-password` | Public |  | JSON: `email`* | Send a password-recovery email |
| POST | `/api/v1/auth/login` | Public |  | JSON: `email`*, `password`* | Sign in with email and password |
| POST | `/api/v1/auth/logout` | Authenticated |  |  | Revoke the current session |
| GET | `/api/v1/auth/me` | Authenticated |  |  | Current authenticated user |
| POST | `/api/v1/auth/refresh` | Public |  | JSON: `refresh_token`* | Exchange a refresh token for new tokens |
| POST | `/api/v1/auth/resend-verification` | Public |  | JSON: `email`* | Resend the signup confirmation email |
| POST | `/api/v1/auth/reset-password` | Public (recovery token) |  | JSON: `new_password`*, `token_hash` | Set a new password after recovery |
| POST | `/api/v1/auth/signup` | Public |  | JSON: `email`*, `password`*, `full_name` | Register with email and password |

### CV

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/cv` | Authenticated |  |  | List my CVs |
| POST | `/api/v1/cv/upload` | Authenticated |  | multipart: `file`* | Upload a CV (PDF or DOCX) |
| DELETE | `/api/v1/cv/{cv_id}` | Authenticated | `cv_id`* |  | Delete a CV |
| GET | `/api/v1/cv/{cv_id}` | Authenticated | `cv_id`* |  | Get CV metadata |
| POST | `/api/v1/cv/{cv_id}/apply-to-profile` | Authenticated | `cv_id`* | JSON: `fields`, `overwrite` | Copy extracted CV data into my profile |
| GET | `/api/v1/cv/{cv_id}/download` | Authenticated | `cv_id`* |  | Get a short-lived download URL |
| GET | `/api/v1/cv/{cv_id}/extracted-data` | Authenticated | `cv_id`* |  | Get extracted CV data |
| PATCH | `/api/v1/cv/{cv_id}/extracted-data` | Authenticated | `cv_id`* | JSON: `extracted_name`, `skills`, `job_titles`, `years_of_experience`, `certifications`, `education`, `work_experience` | Review/correct extracted data (marks it user-confirmed) |
| POST | `/api/v1/cv/{cv_id}/primary` | Authenticated | `cv_id`* |  | Make this my primary CV |
| POST | `/api/v1/cv/{cv_id}/process` | Authenticated | `cv_id`* |  | Re-run CV processing |

### Employer Jobs & Applicants

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/employer/applications` | Employer (with company profile) | `job_id`, `status`, `sort`, `page`, `page_size` |  | All applicants to my jobs |
| GET | `/api/v1/employer/applications/{application_id}` | Employer (with company profile) | `application_id`* |  | One applicant |
| GET | `/api/v1/employer/applications/{application_id}/cv` | Employer (with company profile) | `application_id`* |  | Applicant CV (signed URL) |
| PATCH | `/api/v1/employer/applications/{application_id}/status` | Employer (with company profile) | `application_id`* | JSON: `status`*, `notes` | Update employer review status |
| GET | `/api/v1/employer/jobs` | Employer (with company profile) | `status`, `page`, `page_size` |  | My job postings |
| POST | `/api/v1/employer/jobs` | Employer (with company profile) |  | JSON: `title`*, `description`*, `required_skills`, `preferred_skills`, `experience_level`, `min_experience_years`, `max_experience_years`, `employment_type`*, `workplace_type`, `city`, `location`, `country`, `min_salary`, `max_salary`, `salary_currency`, `salary_period`, `education_requirements`, `application_deadline`, `application_instructions`, `external_application_url`, `openings`, `benefits`, `submit_for_review` | Create a job posting |
| DELETE | `/api/v1/employer/jobs/{job_id}` | Employer (with company profile) | `job_id`* |  | Delete or archive a job |
| GET | `/api/v1/employer/jobs/{job_id}` | Employer (with company profile) | `job_id`* |  | One of my job postings |
| PUT | `/api/v1/employer/jobs/{job_id}` | Employer (with company profile) | `job_id`* | JSON: `title`*, `description`*, `required_skills`, `preferred_skills`, `experience_level`, `min_experience_years`, `max_experience_years`, `employment_type`*, `workplace_type`, `city`, `location`, `country`, `min_salary`, `max_salary`, `salary_currency`, `salary_period`, `education_requirements`, `application_deadline`, `application_instructions`, `external_application_url`, `openings`, `benefits` | Update a job posting |
| GET | `/api/v1/employer/jobs/{job_id}/applicants` | Employer (with company profile) | `job_id`*; `status`, `sort`, `page`, `page_size` |  | Applicants for a job |
| PATCH | `/api/v1/employer/jobs/{job_id}/status` | Employer (with company profile) | `job_id`* | JSON: `status`*, `reason` | Change job posting status |
| GET | `/api/v1/employer/statistics` | Employer (with company profile) |  |  | My hiring statistics |

### Employers

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/employers/me` | Employer (with company profile) |  |  | My company profile |
| PUT | `/api/v1/employers/me` | Employer (with company profile) |  | JSON: `company_name`*, `description`, `industry`, `website`, `company_size`, `headquarters`, `country`, `contact_email`*, `contact_phone`, `social_links` | Update my company profile |
| DELETE | `/api/v1/employers/me/logo` | Employer (with company profile) |  |  | Remove company logo |
| POST | `/api/v1/employers/me/logo` | Employer (with company profile) |  | multipart: `file`* | Upload company logo |
| GET | `/api/v1/employers/me/verification` | Employer (with company profile) |  |  | Verification status |
| POST | `/api/v1/employers/me/verification` | Employer (with company profile) |  | JSON: `business_registration_number`, `company_website`, `official_email`, `additional_information` | Submit (or resubmit) verification |
| POST | `/api/v1/employers/register` | Authenticated (job seeker → becomes employer) |  | JSON: `company_name`*, `description`, `industry`, `website`, `company_size`, `headquarters`, `country`, `contact_email`*, `contact_phone`, `social_links` | Register a company (becomes an employer account) |
| GET | `/api/v1/employers/{employer_id}` | Authenticated | `employer_id`* |  | Public company profile |

### Health

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/health` | Public |  |  | API liveness |
| GET | `/api/v1/health/database` | Public |  |  | Database readiness |
| GET | `/health` | Public |  |  | Application liveness |

### Interviews

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/interviews/upcoming` | Authenticated | `days` |  | Upcoming interviews |
| DELETE | `/api/v1/interviews/{interview_id}` | Authenticated | `interview_id`* |  | Delete an interview |
| PUT | `/api/v1/interviews/{interview_id}` | Authenticated | `interview_id`* | JSON: `interview_type`*, `interview_round`, `scheduled_at`*, `timezone`, `duration_minutes`, `location_or_meeting_link`, `interviewer_name`, `notes`, `outcome` | Update an interview |

### Jobs

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/jobs/filters` | Public |  |  | Supported search filters |
| GET | `/api/v1/jobs/search` | Authenticated | `query`*, `location`, `country`, `company`, `remote_jobs_only`, `employment_types`, `experience`, `date_posted`, `radius`, `language`, `exclude_job_publishers`, `min_salary`, `num_pages`, `cursor` |  | Search jobs (Pakistan, international, remote) |
| GET | `/api/v1/jobs/sharkhire` | Authenticated | `q`, `location`, `remote`, `employment_type`, `page`, `page_size` |  | Jobs posted on SharkHire |
| GET | `/api/v1/jobs/suggestions` | Authenticated | `q`*, `limit` |  | Job title suggestions |
| GET | `/api/v1/jobs/{job_id}` | Authenticated | `job_id`*; `refresh` |  | Job details |

### Profile

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/profile/me` | Authenticated |  |  | Get my profile |
| PATCH | `/api/v1/profile/me` | Authenticated |  | JSON: `full_name`, `phone`, `headline`, `location`, `country`, `city`, `professional_summary`, `current_job_title`, `years_of_experience`, `education`, `experience`, `skills`, `certifications`, `preferred_job_titles`, `preferred_employment_types`, `preferred_locations` | Partially update my profile |
| PUT | `/api/v1/profile/me` | Authenticated |  | JSON: `full_name`, `phone`, `headline`, `location`, `country`, `city`, `professional_summary`, `current_job_title`, `years_of_experience`, `education`, `experience`, `skills`, `certifications`, `preferred_job_titles`, `preferred_employment_types`, `preferred_locations` | Create or replace my profile |
| DELETE | `/api/v1/profile/me/photo` | Authenticated |  |  | Remove my profile photo |
| POST | `/api/v1/profile/me/photo` | Authenticated |  | multipart: `file`* | Upload or replace my profile photo |

### Recommendations

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| POST | `/api/v1/recommendations/analyze` | Authenticated |  | JSON: `cv_id`*, `page`, `page_size`, `min_score`, `remote_only` | Analyse a specific CV against jobs |
| GET | `/api/v1/recommendations/for-me` | Authenticated | `cv_id`, `page`, `page_size`, `min_score`, `remote_only` |  | Personalised recommendations |
| GET | `/api/v1/recommendations/history` | Authenticated | `page`, `page_size` |  | Previously generated recommendations |
| GET | `/api/v1/recommendations/jobs/{job_id}/match` | Authenticated | `job_id`*; `cv_id` |  | Match one job against my CV |
| GET | `/api/v1/recommendations/preferences` | Authenticated |  |  | Get recommendation preferences |
| PUT | `/api/v1/recommendations/preferences` | Authenticated |  | JSON: `preferred_job_titles`, `preferred_locations`, `preferred_countries`, `employment_types`, `remote_preference`, `experience_level`, `min_match_score` | Replace recommendation preferences |

### Reports

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| POST | `/api/v1/reports` | Authenticated |  | JSON: `target_type`*, `target_id`*, `reason`*, `description` | Report a suspicious job or employer |
| GET | `/api/v1/reports/mine` | Authenticated | `page`, `page_size` |  | My reports |

### Saved Jobs

| Method | Path | Access | Path / query params | Body | Summary |
|---|---|---|---|---|---|
| GET | `/api/v1/saved-jobs` | Authenticated | `q`, `company`, `location`, `remote`, `employment_type`, `sort`, `page`, `page_size` |  | List my saved jobs |
| GET | `/api/v1/saved-jobs/check/{job_id}` | Authenticated | `job_id`* |  | Is this job saved? |
| DELETE | `/api/v1/saved-jobs/{job_id}` | Authenticated | `job_id`* |  | Unsave a job |
| GET | `/api/v1/saved-jobs/{job_id}` | Authenticated | `job_id`* |  | Get one saved job |
| POST | `/api/v1/saved-jobs/{job_id}` | Authenticated | `job_id`* | JSON: `notes` | Save a job |
