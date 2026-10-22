import { readFile } from 'node:fs/promises';

const candidates = [
  new URL('../docs/data/projects.json', import.meta.url),
  new URL('../data/projects.json', import.meta.url)
];
let source;
for (const candidate of candidates) {
  try { source = await readFile(candidate, 'utf8'); break; } catch (_) { /* Try the next known project-data location. */ }
}
if (source === undefined) throw new Error('Could not find docs/data/projects.json');
const data = JSON.parse(source);
const allowedGroups = new Set(['FFA', 'OEV', 'Personal']);
const allowedHosts = new Set(['docs.google.com', 'github.com']);
const forbiddenKeys = new Set(['tasks', 'status', 'badge', 'next_step', 'owner', 'note', 'start', 'end', 'depends_on', 'inbox']);
const fail = (message) => { console.error(`Project directory validation failed: ${message}`); process.exit(1); };

if (data.schema_version !== 1) fail('unsupported schema_version');
if (!Array.isArray(data.groups) || !Array.isArray(data.projects)) fail('groups and projects must be arrays');
if (new Set(data.groups).size !== data.groups.length) fail('duplicate groups');
if (data.groups.some((group) => !allowedGroups.has(group))) fail('unknown group');
if (data.projects.length === 0) fail('no projects found');

const ids = new Set();
function validateLink(link, where) {
  if (!link || typeof link.url !== 'string' || !['Google', 'GitHub'].includes(link.provider)) fail(`${where}: invalid link record`);
  let url;
  try { url = new URL(link.url); } catch { fail(`${where}: invalid URL`); }
  if (url.protocol !== 'https:' || !allowedHosts.has(url.hostname)) fail(`${where}: URL must use an approved HTTPS host`);
}

for (const project of data.projects) {
  for (const key of Object.keys(project)) if (forbiddenKeys.has(key)) fail(`${project.id || 'project'} exposes prohibited field: ${key}`);
  if (!project.id || !project.name || !project.mono || !allowedGroups.has(project.group)) fail('project is missing a required public directory field');
  if (ids.has(project.id)) fail(`duplicate project id: ${project.id}`);
  ids.add(project.id);
  validateLink(project.workspace, `${project.id}.workspace`);
  if (project.roadmap) validateLink(project.roadmap, `${project.id}.roadmap`);
}

console.log(`Validated ${data.projects.length} public project entries; no task or roadmap detail fields found.`);
