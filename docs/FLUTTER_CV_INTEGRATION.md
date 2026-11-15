# Flutter ↔ CV Upload & Review

Same API for Android and iOS. Reference: [PROFILE_CV_API.md](PROFILE_CV_API.md).

## 1. Pick a file
Use `file_picker` with `allowedExtensions: ['pdf', 'docx']`, `type: FileType.custom`. Check size client-side
(≤ 5 MB) for fast feedback — the server enforces it anyway.

## 2–4. Upload with progress (multipart + Bearer)
`http.MultipartRequest` has no progress callback; use `dio`:
```dart
final dio = Dio(BaseOptions(baseUrl: baseUrl));
final form = FormData.fromMap({
  'file': await MultipartFile.fromFile(path, filename: name,
      contentType: DioMediaType.parse(name.endsWith('.pdf') ? 'application/pdf'
          : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
});
final res = await dio.post('/cv/upload', data: form,
    options: Options(headers: {'Authorization': 'Bearer $token'}),
    onSendProgress: (sent, total) => setState(() => progress = sent / total));
final cv = res.data['data']; // id, processing_status, skills_found, is_primary ...
```

## 5. Errors
```dart
on DioException catch (e) {
  final code = e.response?.data?['error']?['code'];
  switch (code) {
    case 'file_too_large': show('Max 5 MB'); break;
    case 'unsupported_file_type': case 'invalid_file_signature': case 'mime_mismatch':
      show('Please upload a PDF or Word (.docx) file'); break;
    case 'duplicate_cv': show('You already uploaded this file'); break;
    case 'cv_limit_reached': show('Delete an old CV first'); break;
    case 'corrupted_file': show('The file seems damaged'); break;
    default: show(e.response?.data?['message'] ?? 'Upload failed');
  }
}
```

## 6. Status
If `processing_status` is `ocr_required` tell the user the PDF is scanned and ask for a text-based PDF/DOCX;
if `failed`, show `extraction_error` (e.g. password-protected).

## 7. Show extracted skills
```dart
final r = await http.get(Uri.parse('$baseUrl/cv/$cvId/extracted-data'), headers: await authHeaders());
final d = jsonDecode(r.body)['data'];
// d['skills'], d['unrecognized_skills'], d['work_experience'], d['education'],
// d['years_of_experience'], d['extraction_metadata']['confidence']
```

## 8. Let the user review
Corrections mark the data as confirmed (preferred by recommendations):
```dart
await http.patch(Uri.parse('$baseUrl/cv/$cvId/extracted-data'), headers: await authHeaders(),
    body: jsonEncode({'skills': editedSkills, 'years_of_experience': 4}));
// Optionally copy into the profile:
await http.post(Uri.parse('$baseUrl/cv/$cvId/apply-to-profile'), headers: await authHeaders(),
    body: jsonEncode({'fields': ['skills', 'experience', 'education'], 'overwrite': false}));
```

## 9. Secure download
`GET /cv/{id}/download` → `{url, expires_in, filename}`. Open/download the URL immediately (it expires after
~5 minutes). Do not store or share it.

## 10. Delete
`DELETE /cv/{id}` → removes the file and extracted data. If it was primary, the newest remaining CV becomes primary.
