-- SQLite
SELECT u.id, u.file_name, u.created_at, length(e.extracted_text) AS 글자수
FROM uploaded_files u LEFT JOIN extracted_texts e ON e.file_id = u.id
ORDER BY u.id;