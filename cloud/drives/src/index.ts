import { timingSafeEqual } from 'node:crypto';

// Whole-drive harvesting for model training. One manifest per loggerd segment;
// parts are checksummed by R2 while streaming and never buffered in the Worker.
const ROUTE = /^[A-Za-z0-9|_-]{1,128}$/;
const NAME = /^[a-zA-Z0-9_.-]{1,100}$/;
const HASH = /^[a-f0-9]{64}$/;
const PART = 16 * 1024 * 1024;
const MAX_MANIFEST = 131072;
// Road/wide video and logs only. Cabin video and audio are never accepted.
const ARTIFACTS = new Set(['fcamera.hevc','ecamera.hevc','rlog.zst','qlog.zst','qcamera.ts']);
type Part = {size:number; sha256:string};
type Artifact = {name:string; size:number; sha256:string; parts:Part[]};
type Manifest = {route:string; segment:number; created_at:number; commit:string; branch?:string; version?:string; dirty?:boolean; artifacts:Artifact[]};

function validManifest(x: unknown): x is Manifest {
  if (!x || typeof x !== 'object') return false;
  const m = x as Partial<Manifest>;
  if (typeof m.route !== 'string' || !ROUTE.test(m.route) || !Number.isSafeInteger(m.segment) || (m.segment as number) < 0 ||
      typeof m.created_at !== 'number' || !Number.isFinite(m.created_at) ||
      typeof m.commit !== 'string' || !/^[a-f0-9]{7,40}$|^$/.test(m.commit) ||
      !Array.isArray(m.artifacts) || m.artifacts.length === 0 || m.artifacts.length > ARTIFACTS.size) return false;
  const names = new Set<string>();
  let parts = 0;
  for (const a of m.artifacts) {
    if (!a || typeof a.name !== 'string' || !ARTIFACTS.has(a.name) || names.has(a.name) ||
        !Number.isSafeInteger(a.size) || a.size < 0 || typeof a.sha256 !== 'string' || !HASH.test(a.sha256) ||
        !Array.isArray(a.parts) || a.parts.length === 0 || a.parts.length > 64) return false;
    names.add(a.name);
    let size = 0;
    for (const p of a.parts) {
      if (!p || !Number.isSafeInteger(p.size) || p.size < 0 || p.size > PART || typeof p.sha256 !== 'string' || !HASH.test(p.sha256)) return false;
      size += p.size; parts++;
    }
    if (size !== a.size) return false;
  }
  return parts <= 256;
}

function reply(body: unknown, status = 200) {
  return Response.json(body, {status, headers:{'Cache-Control':'no-store'}});
}

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return reply({ok:true, service:'funnypilot-drives'});
    // Upload-only credential: no endpoint here reads recordings back.
    const token = request.headers.get('Authorization')?.replace(/^Bearer /, '') || '';
    const expected = env.UPLOAD_TOKEN || '';
    if (!expected || !HASH.test(token) || token.length !== expected.length || !timingSafeEqual(new TextEncoder().encode(token), new TextEncoder().encode(expected))) return reply({error:'unauthorized'},401);
    const match = /^\/segments\/([^/]+)\/(\d{1,6})(?:\/(complete|files)\/?)?(?:\/([a-zA-Z0-9_.-]+)\/(\d+))?$/.exec(url.pathname);
    if (!match) return reply({error:'not found'},404);
    let route: string;
    try { route = decodeURIComponent(match[1]); } catch { return reply({error:'not found'},404); }
    if (!ROUTE.test(route)) return reply({error:'not found'},404);
    const segment = Number(match[2]);
    const [, , , action, name, partText] = match;
    const prefix = `drives/${route}/${segment}/`;
    try {
      if (!action && request.method === 'PUT') {
        const length = Number(request.headers.get('Content-Length'));
        if (!length || length > MAX_MANIFEST) return reply({error:'manifest too large'},413);
        const reader = request.body?.getReader();
        if (!reader) return reply({error:'missing body'},400);
        let size=0; const chunks:Uint8Array[]=[];
        while (true) {const r=await reader.read(); if(r.done) break; size+=r.value.byteLength; if(size>MAX_MANIFEST) {await reader.cancel(); return reply({error:'manifest too large'},413);} chunks.push(r.value);}
        const bytes=new Uint8Array(size); let offset=0; for(const c of chunks){bytes.set(c,offset);offset+=c.byteLength;}
        const m:unknown = JSON.parse(new TextDecoder().decode(bytes));
        if (!validManifest(m) || m.route !== route || m.segment !== segment) return reply({error:'invalid manifest'},400);
        // Once complete, retries are accepted only for the identical manifest.
        const old = await env.DB.prepare('SELECT manifest,upload_status FROM segments WHERE route=? AND segment=?').bind(route,segment).first<{manifest:string;upload_status:string}>();
        const encoded=JSON.stringify(m);
        if (old?.upload_status === 'complete') return old.manifest === encoded ? reply({ok:true}) : reply({error:'segment already complete'},409);
        const total = m.artifacts.reduce((n,a)=>n+a.size,0);
        await env.DB.prepare("INSERT INTO segments(route,segment,created_at,commit_sha,bytes,manifest) VALUES(?,?,?,?,?,?) ON CONFLICT(route,segment) DO UPDATE SET created_at=excluded.created_at,commit_sha=excluded.commit_sha,bytes=excluded.bytes,manifest=excluded.manifest")
          .bind(route,segment,m.created_at,m.commit,total,encoded).run();
        return reply({ok:true});
      }
      const row = await env.DB.prepare('SELECT manifest,upload_status FROM segments WHERE route=? AND segment=?').bind(route,segment).first<{manifest:string;upload_status:string}>();
      if (!row) return reply({error:'send manifest first'},404);
      const m:Manifest=JSON.parse(row.manifest);
      if (action === 'files' && name && partText && request.method === 'PUT') {
        const a=m.artifacts.find(a=>a.name===name); const index=Number(partText); const p=a?.parts[index];
        if (!p || !Number.isSafeInteger(index)) return reply({error:'unknown artifact'},400);
        if (row.upload_status === 'complete') return reply({ok:true});
        if (Number(request.headers.get('Content-Length')) !== p.size) return reply({error:'length mismatch'},400);
        const object=await env.ARTIFACTS.put(`${prefix}${name}/${index}`, request.body,
          {sha256:p.sha256, customMetadata:{sha256:p.sha256}, httpMetadata:{contentType:'application/octet-stream'}});
        if (!object || object.size !== p.size) return reply({error:'artifact mismatch'},400);
        return reply({ok:true});
      }
      if (action === 'complete' && request.method === 'POST') {
        // One list call (not a head per part) keeps this inside subrequest limits.
        const found = new Map<string,{size:number;sha:string|undefined}>();
        let cursor: string|undefined;
        do {
          const page = await env.ARTIFACTS.list({prefix, cursor, include:['customMetadata']});
          for (const o of page.objects) found.set(o.key, {size:o.size, sha:o.customMetadata?.sha256});
          cursor = page.truncated ? page.cursor : undefined;
        } while (cursor);
        for(const a of m.artifacts) for(let i=0;i<a.parts.length;i++) {
          const p=a.parts[i], obj=found.get(`${prefix}${a.name}/${i}`);
          if(!obj || obj.size!==p.size || obj.sha!==p.sha256) return reply({error:'upload incomplete'},409);
        }
        await env.DB.prepare("UPDATE segments SET upload_status='complete' WHERE route=? AND segment=?").bind(route,segment).run();
        return reply({ok:true});
      }
      return reply({error:'method not allowed'},405);
    } catch {
      // No request bodies, route locations or auth headers in platform logs.
      return reply({error:'upload failed; retry later'},503);
    }
  },
} satisfies ExportedHandler<Env>;
