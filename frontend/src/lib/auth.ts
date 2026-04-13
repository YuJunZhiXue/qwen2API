export function getAuthHeader() {
  const key = localStorage.getItem('qwen2api_key') || '';
  return key ? { Authorization: `Bearer ${key}` } : {};
}
