import { timingSafeEqual } from 'node:crypto';

const LABELS = new Set(['steering_bite','steering_wander','unnecessary_slowdown','late_braking','harsh_braking','slow_response']);
const UUID = /^[a-f0-9]{32}$/;
const NAME = /^[a-zA-Z0-9_.-]{1,100}$/;
const HASH = /^[a-f0-9]{64}$/;
const CHUNK = 4 * 1024 * 1024;
type Part = {size:number; sha256:string};
type Artifact = {name:string; size:number; sha256:string; parts:Part[]};
type Manifest = {id:string; route:string; created_at:number; commit:string; labels:string[]; artifacts:Artifact[]};

function validManifest(x: unknown): x is Manifest {
  if (!x || typeof x !== 'object') return false;
  const m = x as Partial<Manifest>;
  if (typeof m.id !== 'string' || !UUID.test(m.id) || typeof m.route !== 'string' || !/^[A-Za-z0-9|_-]{1,128}$/.test(m.route) ||
      typeof m.created_at !== 'number' || !Number.isFinite(m.created_at) || typeof m.commit !== 'string' || !/^[a-f0-9]{7,40}$/.test(m.commit) ||
      !Array.isArray(m.labels) || m.labels.length > 6 || !m.labels.every(l=>LABELS.has(l)) ||
      !Array.isArray(m.artifacts) || m.artifacts.length > 12) return false;
  const names = new Set<string>();
  let total = 0;
  for (const a of m.artifacts) {
    if (!a || typeof a.name !== 'string' || !NAME.test(a.name) || a.name === '.' || a.name === '..' || names.has(a.name) ||
        !Number.isSafeInteger(a.size) || a.size < 0 || typeof a.sha256 !== 'string' || !HASH.test(a.sha256) ||
        !Array.isArray(a.parts) || a.parts.length > 16 || a.parts.length === 0) return false;
    names.add(a.name);
    let size = 0;
    for (const p of a.parts) {
      if (!p || !Number.isSafeInteger(p.size) || p.size < 0 || p.size > CHUNK || typeof p.sha256 !== 'string' || !HASH.test(p.sha256)) return false;
      size += p.size; total++;
    }
    if (size !== a.size) return false;
  }
  return total <= 64;
}

function reply(body: unknown, status = 200) {
  return Response.json(body, {status, headers:{'Cache-Control':'no-store'}});
}

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return reply({ok:true, service:'funnypilot-feedback'});
    // Upload-only credential: it never permits reading private recordings.
    const token = request.headers.get('Authorization')?.replace(/^Bearer /, '') || '';
    const expected = env.UPLOAD_TOKEN || '';
    if (!expected || !HASH.test(token) || token.length !== expected.length || !timingSafeEqual(new TextEncoder().encode(token), new TextEncoder().encode(expected))) return reply({error:'unauthorized'},401);
    const match = /^\/events\/([a-f0-9]{32})(?:\/(complete|files)\/?)?(?:\/([a-zA-Z0-9_.-]+)\/(\d+))?$/.exec(url.pathname);
    if (!match) return reply({error:'not found'},404);
    const [, id, action, name, partText] = match;
    try {
      if (!action && request.method === 'PUT') {
        const length = Number(request.headers.get('Content-Length'));
        if (!length || length > 65536) return reply({error:'manifest too large'},413);
        const reader = request.body?.getReader();
        if (!reader) return reply({error:'missing body'},400);
        let size=0; const chunks:Uint8Array[]=[];
        while (true) {const r=await reader.read(); if(r.done) break; size+=r.value.byteLength; if(size>65536) {await reader.cancel(); return reply({error:'manifest too large'},413);} chunks.push(r.value);}
        const bytes=new Uint8Array(size); let offset=0; for(const c of chunks){bytes.set(c,offset);offset+=c.byteLength;}
        const m:unknown = JSON.parse(new TextDecoder().decode(bytes));
        if (!validManifest(m) || m.id !== id) return reply({error:'invalid manifest'},400);
        // Once complete, retries are accepted only for the identical manifest.
        const old = await env.DB.prepare('SELECT manifest,upload_status FROM events WHERE id=?').bind(id).first<{manifest:string;upload_status:string}>();
        const encoded=JSON.stringify(m);
        if (old?.upload_status === 'complete') return old.manifest === encoded ? reply({ok:true}) : reply({error:'event already complete'},409);
        await env.DB.batch([
          env.DB.prepare("INSERT INTO events(id,route,created_at,commit_sha,labels,manifest) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET labels=excluded.labels,manifest=excluded.manifest")
            .bind(id,m.route,m.created_at,m.commit,JSON.stringify(m.labels),encoded),
          env.DB.prepare('DELETE FROM event_labels WHERE event_id=?').bind(id),
          ...m.labels.map(l=>env.DB.prepare('INSERT OR IGNORE INTO event_labels(event_id,label) VALUES(?,?)').bind(id,l)),
        ]);
        return reply({ok:true});
      }
      const row = await env.DB.prepare('SELECT manifest,upload_status FROM events WHERE id=?').bind(id).first<{manifest:string;upload_status:string}>();
      if (!row) return reply({error:'send manifest first'},404);
      const m:Manifest=JSON.parse(row.manifest);
      if (action === 'files' && name && partText && request.method === 'PUT') {
        const a=m.artifacts.find(a=>a.name===name); const index=Number(partText); const p=a?.parts[index];
        if (!p || !Number.isSafeInteger(index)) return reply({error:'unknown artifact'},400);
        if (row.upload_status === 'complete') return reply({ok:true});
        if (Number(request.headers.get('Content-Length')) !== p.size) return reply({error:'length mismatch'},400);
        // R2 validates length/checksum while streaming; never buffer video in a Worker.
        const object=await env.ARTIFACTS.put(`events/${id}/${name}/${index}`, request.body,
          {sha256:p.sha256, customMetadata:{sha256:p.sha256}, httpMetadata:{contentType:'application/octet-stream'}});
        if (!object || object.size !== p.size) return reply({error:'artifact mismatch'},400);
        return reply({ok:true});
      }
      if (action === 'complete' && request.method === 'POST') {
        for(const a of m.artifacts) for(let i=0;i<a.parts.length;i++) {
          const p=a.parts[i], obj=await env.ARTIFACTS.head(`events/${id}/${a.name}/${i}`);
          if(!obj || obj.size!==p.size || obj.customMetadata?.sha256!==p.sha256) return reply({error:'upload incomplete'},409);
        }
        await env.DB.prepare("UPDATE events SET upload_status='complete' WHERE id=?").bind(id).run();
        return reply({ok:true});
      }
      return reply({error:'method not allowed'},405);
    } catch {
      // No request bodies, route locations or auth headers in platform logs.
      return reply({error:'upload failed; retry later'},503);
    }
  },
} satisfies ExportedHandler<Env>;
