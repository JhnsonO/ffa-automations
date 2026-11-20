const app = document.querySelector('#app');
const groupNav = document.querySelector('#group-nav');
const mobileGroupNav = document.querySelector('#mobile-group-nav');
const projectRoot = document.querySelector('#projects');
const countLabel = document.querySelector('#project-count');
const searchInput = document.querySelector('#project-search');
const groupNames = ['All projects', 'FFA', 'OEV', 'Personal'];
let projects = [];
let selectedGroup = 'All';
let searchTerm = '';
const allowedHosts = { Google: 'docs.google.com', GitHub: 'github.com' };

function validLink(link) {
  if (!link || Object.keys(link).some((key) => !['provider', 'url'].includes(key))) return false;
  if (!Object.hasOwn(allowedHosts, link.provider) || typeof link.url !== 'string') return false;
  try {
    const url = new URL(link.url);
    return url.protocol === 'https:' && url.hostname === allowedHosts[link.provider];
  } catch (_) { return false; }
}

function validDirectory(data) {
  if (!Array.isArray(data.projects) || !Array.isArray(data.groups)) return false;
  const ids = new Set();
  return data.projects.every((project) => {
    const keys = Object.keys(project);
    if (keys.some((key) => !['id', 'name', 'mono', 'group', 'workspace', 'roadmap'].includes(key))) return false;
    if (!project.id || !project.name || !project.mono || !groupNames.slice(1).includes(project.group) || ids.has(project.id)) return false;
    if (!validLink(project.workspace) || (project.roadmap && !validLink(project.roadmap))) return false;
    ids.add(project.id);
    return true;
  });
}

function setTheme(theme) {
  if (!['light', 'dark', 'char'].includes(theme)) theme = 'dark';
  app.dataset.theme = theme;
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
  });
  try { localStorage.setItem('johnson-project-hub-theme', theme); } catch (_) { /* Theme still works for this visit. */ }
}

function getInitialTheme() {
  try {
    const saved = localStorage.getItem('johnson-project-hub-theme');
    return ['light', 'dark', 'char'].includes(saved) ? saved : 'dark';
  } catch (_) { return 'dark'; }
}

function makeGroupButton(name, mobile = false) {
  const key = name === 'All projects' ? 'All' : name;
  const count = key === 'All' ? projects.length : projects.filter((project) => project.group === key).length;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'group-filter';
  button.dataset.group = key;
  button.setAttribute('aria-pressed', String(selectedGroup === key));
  const left = document.createElement('span');
  left.className = 'group-left';
  const square = document.createElement('span');
  square.className = 'group-square';
  square.setAttribute('aria-hidden', 'true');
  const label = document.createElement('span');
  label.textContent = name;
  left.append(square, label);
  button.append(left);
  if (!mobile) {
    const n = document.createElement('span');
    n.className = 'group-count';
    n.textContent = String(count);
    button.append(n);
  }
  button.addEventListener('click', () => {
    selectedGroup = key;
    renderGroups();
  });
  return button;
}

function safeExternalLink(link, className, label) {
  const anchor = document.createElement('a');
  anchor.className = className;
  anchor.href = link.url;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  const text = document.createElement('span');
  text.textContent = label;
  const arrow = document.createElement('span');
  arrow.setAttribute('aria-hidden', 'true');
  arrow.textContent = '↗';
  anchor.append(text, arrow);
  return anchor;
}

function makeProjectCard(project) {
  const card = document.createElement('article');
  card.className = 'project-card';
  const mono = document.createElement('span');
  mono.className = 'project-mono';
  mono.setAttribute('aria-hidden', 'true');
  mono.textContent = project.mono;
  const info = document.createElement('div');
  const title = document.createElement('h4');
  title.className = 'project-title';
  title.textContent = project.name;
  const meta = document.createElement('div');
  meta.className = 'project-meta';
  const group = document.createElement('span');
  group.textContent = project.group;
  const sep = document.createElement('span');
  sep.className = 'meta-sep';
  sep.textContent = '//';
  const provider = document.createElement('span');
  provider.textContent = `${project.workspace.provider} sign-in`;
  meta.append(group, sep, provider);
  info.append(title, meta);
  const actions = document.createElement('div');
  actions.className = 'project-actions';
  const link = safeExternalLink(project.workspace, 'workspace-link', 'Open private workspace');
  link.setAttribute('aria-label', `Sign in and open ${project.name} private workspace`);
  actions.append(link);
  if (project.roadmap) {
    const roadmap = safeExternalLink(project.roadmap, 'workspace-link roadmap-link', 'Open roadmap');
    roadmap.setAttribute('aria-label', `Sign in and open ${project.name} roadmap`);
    actions.append(roadmap);
  }
  card.append(mono, info, actions);
  return card;
}

function renderGroups() {
  groupNav.replaceChildren(...groupNames.map((name) => makeGroupButton(name)));
  mobileGroupNav.replaceChildren(...groupNames.map((name) => makeGroupButton(name, true)));
  const needle = searchTerm.trim().toLocaleLowerCase();
  const filtered = projects.filter((project) => {
    const matchesGroup = selectedGroup === 'All' || project.group === selectedGroup;
    const matchesSearch = !needle || `${project.name} ${project.group}`.toLocaleLowerCase().includes(needle);
    return matchesGroup && matchesSearch;
  });
  countLabel.textContent = `${filtered.length} ${filtered.length === 1 ? 'workspace' : 'workspaces'}`;
  projectRoot.replaceChildren();
  const displayedGroups = [...new Set(filtered.map((project) => project.group))];
  displayedGroups.forEach((groupName) => {
    const group = document.createElement('section');
    group.className = 'project-group';
    const heading = document.createElement('div');
    heading.className = 'group-heading';
    const title = document.createElement('h3');
    title.textContent = groupName;
    const n = document.createElement('span');
    const groupProjects = filtered.filter((project) => project.group === groupName);
    n.textContent = `${String(groupProjects.length).padStart(2, '0')} PROJECT${groupProjects.length === 1 ? '' : 'S'}`;
    heading.append(title, n);
    group.append(heading, ...groupProjects.map(makeProjectCard));
    projectRoot.append(group);
  });
  if (!filtered.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = 'No matching projects. Clear the search or choose another group.';
    projectRoot.append(empty);
  }
}

function showLoadError() {
  projectRoot.innerHTML = '<div class="error-state"><strong>Project directory unavailable</strong>Refresh the page in a moment. The private project boards are not affected.</div>';
  countLabel.textContent = 'Unavailable';
}

document.querySelectorAll('[data-theme-choice]').forEach((button) => {
  button.addEventListener('click', () => setTheme(button.dataset.themeChoice));
});
setTheme(getInitialTheme());
searchInput.addEventListener('input', () => {
  searchTerm = searchInput.value;
  renderGroups();
});
document.querySelector('#access-toggle').addEventListener('click', (event) => {
  const button = event.currentTarget;
  const open = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(open));
  button.querySelector('.disclosure-mark').textContent = open ? '▾' : '▸';
  document.querySelector('#access-details').hidden = !open;
});

fetch('./data/projects.json', { cache: 'no-store' })
  .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
  .then((data) => {
    if (!validDirectory(data)) throw new Error('Invalid project directory');
    projects = data.projects;
    renderGroups();
  })
  .catch(showLoadError);
