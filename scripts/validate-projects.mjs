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
const allowedHosts = new Map([['Google', 'docs.google.com'], ['GitHub', 'github.com']]);
const projectKeys = new Set(['id', 'name', 'mono', 'group', 'workspace', 'roadmap']);
const linkKeys = new Set(['provider', 'url']);
const fail = (message) => { console.error(`Project directory validation failed: ${message}`); process.exit(1); };

if (data.schema_version !== 1) fail('unsupported schema_version');
if (!Array.isArray(data.groups) || !Array.isArray(data.projects)) fail('groups and projects must be arrays');
if (new Set(data.groups).size !== data.groups.length) fail('duplicate groups');
if (data.groups.some((group) => !allowedGroups.has(group))) fail('unknown group');
if (data.projects.length === 0) fail('no projects found');

const ids = new Set();
function validateLink(link, where) {
  if (!link || typeof link.url !== 'string' || !allowedHosts.has(link.provider)) fail(`${where}: invalid link record`);
  if (Object.keys(link).some((key) => !linkKeys.has(key))) fail(`${where}: unexpected link field`);
  let url;
  try { url = new URL(link.url); } catch { fail(`${where}: invalid URL`); }
  if (url.protocol !== 'https:' || allowedHosts.get(link.provider) !== url.hostname) fail(`${where}: provider and HTTPS host do not match`);
}

for (const project of data.projects) {
  if (Object.keys(project).some((key) => !projectKeys.has(key))) fail(`${project.id || 'project'} contains an unexpected field`);
  if (!project.id || !project.name || !project.mono || !allowedGroups.has(project.group)) fail('project is missing a required public directory field');
  if (ids.has(project.id)) fail(`duplicate project id: ${project.id}`);
  ids.add(project.id);
  validateLink(project.workspace, `${project.id}.workspace`);
  if (project.roadmap) validateLink(project.roadmap, `${project.id}.roadmap`);
}

console.log(`Validated ${data.projects.length} public project entries; no task or roadmap detail fields found.`);
