export class ApiError extends Error {
  constructor(message, status, code, details) {super(message); this.status = status; this.code = code; this.details = details || [];}
}

/** Calls /api/team. Bodies are JSON; a credential travels only in the body of one PUT and is never kept. */
export async function request(method, path, body) {
  let response;
  try {
    response = await fetch(`/api/team${path}`, {method, headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)});
  } catch {throw new ApiError('Sem conexão com o assistente local.', 0);}
  let data = null;
  try {data = await response.json();} catch {data = null;}
  if (!response.ok) throw new ApiError(data?.error || 'Não foi possível concluir a operação.', response.status, data?.code, data?.details);
  return data;
}

export const api = {
  get: path => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
  patch: (path, body) => request('PATCH', path, body),
  put: (path, body) => request('PUT', path, body),
  del: path => request('DELETE', path),
};
