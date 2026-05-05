// @vitest-environment node
/**
 * Unit tests for pure utility functions embedded in dashboard.html.
 *
 * Strategy: use Node.js `vm` module to execute the <script> block from
 * dashboard.html in an isolated context with minimal browser-API stubs.
 * Functions declared with the `function` keyword at the top level of the
 * script become properties of the VM context and can be called directly.
 */
import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import { readFileSync } from 'fs';
import { createContext, runInContext } from 'vm';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ── Extract the single <script> block that precedes </body> ─────────────────
function extractScript() {
  const html = readFileSync(join(__dirname, 'dashboard.html'), 'utf8');
  // The script block may be followed by panel HTML before </body>, so match
  // non-greedily and take the last (and only) <script>…</script> occurrence.
  const matches = [...html.matchAll(/<script>([\s\S]+?)<\/script>/g)];
  if (!matches.length) throw new Error('Could not find <script> block in dashboard.html');
  return matches[matches.length - 1][1];
}

// ── Minimal stub for a DOM element ──────────────────────────────────────────
function mockElement() {
  return {
    value: '', textContent: '', innerHTML: '', type: 'text',
    style: { display: '' }, className: '', options: [], selectedOptions: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    scrollIntoView() {},
  };
}

// ── Build & populate the VM context once ────────────────────────────────────
let ctx;
beforeAll(() => {
  ctx = createContext({
    // Config stub — mirrors what proxy.py injects via window.JIRA_CFG
    window: { JIRA_CFG: {} },
    // DOM stubs (used by init-time calls: loadCreds, checkProxy, setProxy)
    document: {
      getElementById:   () => mockElement(),
      querySelectorAll: () => ({ forEach() {} }),
      addEventListener: () => {},
      body: { classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } } },
    },
    sessionStorage: { getItem: () => null, setItem() {} },
    // Network – returns a resolved promise so checkProxy() doesn't crash
    fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
    // Timers / misc
    setInterval() { return 0; },
    setTimeout()  { return 0; },
    clearInterval() {},
    alert() {},
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    AbortSignal: { timeout: () => ({}) },
    // JS built-ins that the script uses
    console, String, Number, Boolean, Array, Object, Math, Date, JSON,
    parseInt, parseFloat, isNaN, isFinite, Infinity, NaN,
    RegExp, Error, TypeError, RangeError,
    Promise, Symbol, Set, Map,
    encodeURIComponent, decodeURIComponent,
  });
  runInContext(extractScript(), ctx);
});

// ════════════════════════════════════════════════════════════════════════════
// esc — HTML entity escaping
// ════════════════════════════════════════════════════════════════════════════
describe('esc', () => {
  it('returns empty string for null', () => {
    expect(ctx.esc(null)).toBe('');
  });
  it('returns empty string for empty string', () => {
    expect(ctx.esc('')).toBe('');
  });
  it('escapes <', () => {
    expect(ctx.esc('<b>')).toBe('&lt;b&gt;');
  });
  it('escapes >', () => {
    expect(ctx.esc('a>b')).toBe('a&gt;b');
  });
  it('escapes &', () => {
    expect(ctx.esc('a & b')).toBe('a &amp; b');
  });
  it('escapes double quotes', () => {
    expect(ctx.esc('"hello"')).toBe('&quot;hello&quot;');
  });
  it('passes through plain text unchanged', () => {
    expect(ctx.esc('Hello World')).toBe('Hello World');
  });
  it('escapes a combination of entities', () => {
    expect(ctx.esc('<script>&"</script>')).toBe('&lt;script&gt;&amp;&quot;&lt;/script&gt;');
  });
  it('converts non-string values to string before escaping', () => {
    expect(ctx.esc(42)).toBe('42');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// fmtDate — date display formatting
// ════════════════════════════════════════════════════════════════════════════
describe('fmtDate', () => {
  it('returns "—" for null', () => {
    expect(ctx.fmtDate(null)).toBe('—');
  });
  it('returns "—" for undefined', () => {
    expect(ctx.fmtDate(undefined)).toBe('—');
  });
  it('passes through a YYYY-MM-DD string unchanged', () => {
    expect(ctx.fmtDate('2026-05-15')).toBe('2026-05-15');
  });
  it('truncates a datetime string to the date part', () => {
    expect(ctx.fmtDate('2026-05-15T10:30:00Z')).toBe('2026-05-15');
  });
  it('formats a Date object via toISOString and slices to 10 chars', () => {
    const d = new Date('2026-07-04T00:00:00.000Z');
    expect(ctx.fmtDate(d)).toBe('2026-07-04');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// parseSprintField — Jira sprint custom field parser
// ════════════════════════════════════════════════════════════════════════════
describe('parseSprintField', () => {
  const active = 'com.atlassian.sprint@[id=95,name=Sprint 95,state=ACTIVE]';
  const future = 'com.atlassian.sprint@[id=96,name=Sprint 96,state=FUTURE]';
  const closed = 'com.atlassian.sprint@[id=90,name=Old Sprint,state=CLOSED]';

  it('returns null for null input', () => {
    expect(ctx.parseSprintField(null)).toBeNull();
  });
  it('returns null for empty array', () => {
    expect(ctx.parseSprintField([])).toBeNull();
  });
  it('returns null when only CLOSED sprints are present', () => {
    expect(ctx.parseSprintField([closed])).toBeNull();
  });
  it('parses an ACTIVE sprint and returns state=active', () => {
    const r = ctx.parseSprintField([active]);
    expect(r).toMatchObject({ name: 'Sprint 95', state: 'active', id: 95 });
  });
  it('parses a FUTURE sprint and returns state=future', () => {
    const r = ctx.parseSprintField([future]);
    expect(r).toMatchObject({ name: 'Sprint 96', state: 'future', id: 96 });
  });
  it('ACTIVE takes priority over FUTURE in the same array', () => {
    const r = ctx.parseSprintField([future, active]);
    expect(r.state).toBe('active');
    expect(r.name).toBe('Sprint 95');
  });
  it('returns the first FUTURE when no ACTIVE sprint is present', () => {
    const future2 = 'sprint@[id=97,name=Sprint 97,state=FUTURE]';
    const r = ctx.parseSprintField([future, future2]);
    expect(r.state).toBe('future');
    expect(r.id).toBe(96); // first future wins
  });
  it('wraps a plain non-array value in an array and parses it', () => {
    const r = ctx.parseSprintField(active);
    expect(r.state).toBe('active');
  });
  it('parses sprint id as a number', () => {
    const r = ctx.parseSprintField([active]);
    expect(typeof r.id).toBe('number');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// totalSP — story-point aggregation
// ════════════════════════════════════════════════════════════════════════════
describe('totalSP', () => {
  it('returns 0 for empty array', () => {
    expect(ctx.totalSP([])).toBe(0);
  });
  it('sums numeric sp values', () => {
    expect(ctx.totalSP([{ sp: 3 }, { sp: 5 }, { sp: 2 }])).toBe(10);
  });
  it('treats null sp as 0', () => {
    expect(ctx.totalSP([{ sp: null }, { sp: 4 }])).toBe(4);
  });
  it('treats missing sp as 0', () => {
    expect(ctx.totalSP([{}, { sp: 2 }])).toBe(2);
  });
  it('returns 0 when all items have zero sp', () => {
    expect(ctx.totalSP([{ sp: 0 }, { sp: 0 }])).toBe(0);
  });
  it('handles fractional sp', () => {
    expect(ctx.totalSP([{ sp: 1.5 }, { sp: 0.5 }])).toBeCloseTo(2);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// statusChip — status badge HTML
// ════════════════════════════════════════════════════════════════════════════
describe('statusChip', () => {
  it('applies chip-rfb for "Ready for Build"', () => {
    const h = ctx.statusChip('Ready for Build');
    expect(h).toContain('chip-rfb');
    expect(h).toContain('Ready for Build');
  });
  it('applies chip-build for "Build"', () => {
    expect(ctx.statusChip('Build')).toContain('chip-build');
  });
  it('applies chip-analysis for "Analysis"', () => {
    expect(ctx.statusChip('Analysis')).toContain('chip-analysis');
  });
  it('applies chip-verified for "Verified"', () => {
    expect(ctx.statusChip('Verified')).toContain('chip-verified');
  });
  it('applies chip-build for "In Progress"', () => {
    expect(ctx.statusChip('In Progress')).toContain('chip-build');
  });
  it('applies chip-domain-build for "Domain Build"', () => {
    expect(ctx.statusChip('Domain Build')).toContain('chip-domain-build');
  });
  it('falls back to chip-nosprint for an unknown status', () => {
    expect(ctx.statusChip('Unknown Status')).toContain('chip-nosprint');
  });
  it('escapes the status text in the output', () => {
    const h = ctx.statusChip('<b>Build</b>');
    expect(h).not.toContain('<b>');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// sevChip — severity badge HTML
// ════════════════════════════════════════════════════════════════════════════
describe('sevChip', () => {
  it('returns chip-sev-none for null', () => {
    expect(ctx.sevChip(null)).toContain('chip-sev-none');
  });
  it('returns chip-sev-crit for "1 - Showstopper"', () => {
    expect(ctx.sevChip('1 - Showstopper')).toContain('chip-sev-crit');
  });
  it('returns chip-sev-crit for "Critical"', () => {
    expect(ctx.sevChip('Critical')).toContain('chip-sev-crit');
  });
  it('returns chip-sev-crit for "Showstopper"', () => {
    expect(ctx.sevChip('Showstopper')).toContain('chip-sev-crit');
  });
  it('returns chip-sev-high for "2 - Major"', () => {
    expect(ctx.sevChip('2 - Major')).toContain('chip-sev-high');
  });
  it('returns chip-sev-high for "Major"', () => {
    expect(ctx.sevChip('Major')).toContain('chip-sev-high');
  });
  it('returns chip-sev-high for "Significant"', () => {
    expect(ctx.sevChip('Significant')).toContain('chip-sev-high');
  });
  it('returns chip-sev-med for "3 - Minor"', () => {
    expect(ctx.sevChip('3 - Minor')).toContain('chip-sev-med');
  });
  it('returns chip-sev-low for "4 - Cosmetic"', () => {
    expect(ctx.sevChip('4 - Cosmetic')).toContain('chip-sev-low');
  });
  it('falls back to chip-sev-low for an unknown severity', () => {
    expect(ctx.sevChip('Very Bad')).toContain('chip-sev-low');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// sprintChip — sprint badge HTML
// ════════════════════════════════════════════════════════════════════════════
describe('sprintChip', () => {
  it('returns chip-nosprint for null', () => {
    const h = ctx.sprintChip(null);
    expect(h).toContain('chip-nosprint');
    expect(h).toContain('no sprint');
  });
  it('returns chip-sprint for an active sprint', () => {
    const h = ctx.sprintChip({ name: 'Sprint 95', state: 'active', id: 95 });
    expect(h).toContain('chip-sprint');
    expect(h).toContain('Sprint 95');
  });
  it('chip-sprint does NOT contain "future" class for active sprint', () => {
    const h = ctx.sprintChip({ name: 'Sprint 95', state: 'active', id: 95 });
    expect(h).not.toContain('chip-sprint-future');
  });
  it('returns chip-sprint-future for a future sprint', () => {
    const h = ctx.sprintChip({ name: 'Sprint 96', state: 'future', id: 96 });
    expect(h).toContain('chip-sprint-future');
    expect(h).toContain('Sprint 96');
  });
  it('escapes the sprint name to prevent XSS', () => {
    const h = ctx.sprintChip({ name: '<Script>', state: 'active' });
    expect(h).toContain('&lt;Script&gt;');
    expect(h).not.toContain('<Script>');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// typeChip — issue type badge HTML
// ════════════════════════════════════════════════════════════════════════════
describe('typeChip', () => {
  it('applies chip-type-story for Story', () => {
    expect(ctx.typeChip('Story')).toContain('chip-type-story');
  });
  it('applies chip-type-bug for Bug', () => {
    expect(ctx.typeChip('Bug')).toContain('chip-type-bug');
  });
  it('applies chip-type-fc for Feature Configuration', () => {
    expect(ctx.typeChip('Feature Configuration')).toContain('chip-type-fc');
  });
  it('applies chip-type-task for Task', () => {
    expect(ctx.typeChip('Task')).toContain('chip-type-task');
  });
  it('applies chip-type-td for Technical Debt', () => {
    expect(ctx.typeChip('Technical Debt')).toContain('chip-type-td');
  });
  it('falls back to chip-type-td for an unknown type', () => {
    expect(ctx.typeChip('Mystery Type')).toContain('chip-type-td');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// getW4Bucket — analysis-due risk-radar bucket classification
// ════════════════════════════════════════════════════════════════════════════
describe('getW4Bucket', () => {
  // Build date strings relative to today so tests aren't date-sensitive.
  function offsetDate(n) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  }

  it('returns null for null input', () => {
    expect(ctx.getW4Bucket(null)).toBeNull();
  });
  it('returns null for empty string', () => {
    expect(ctx.getW4Bucket('')).toBeNull();
  });
  it('returns "overdue" for a date yesterday', () => {
    expect(ctx.getW4Bucket(offsetDate(-1))).toBe('overdue');
  });
  it('returns "overdue" for a date 30 days in the past', () => {
    expect(ctx.getW4Bucket(offsetDate(-30))).toBe('overdue');
  });
  it('returns "0-3d" for today', () => {
    expect(ctx.getW4Bucket(offsetDate(0))).toBe('0-3d');
  });
  it('returns "0-3d" for 3 days from now', () => {
    expect(ctx.getW4Bucket(offsetDate(3))).toBe('0-3d');
  });
  it('returns "4-7d" for 4 days from now', () => {
    expect(ctx.getW4Bucket(offsetDate(4))).toBe('4-7d');
  });
  it('returns "4-7d" for 7 days from now', () => {
    expect(ctx.getW4Bucket(offsetDate(7))).toBe('4-7d');
  });
  it('returns "8-14d" for 8 days from now', () => {
    expect(ctx.getW4Bucket(offsetDate(8))).toBe('8-14d');
  });
  it('returns "8-14d" for 14 days from now', () => {
    expect(ctx.getW4Bucket(offsetDate(14))).toBe('8-14d');
  });
  it('returns null for 15 days from now (beyond radar window)', () => {
    expect(ctx.getW4Bucket(offsetDate(15))).toBeNull();
  });
  it('returns null for 30 days from now', () => {
    expect(ctx.getW4Bucket(offsetDate(30))).toBeNull();
  });
});

// ════════════════════════════════════════════════════════════════════════════
// normalizeIssue — Jira API issue → internal object
// ════════════════════════════════════════════════════════════════════════════
describe('normalizeIssue', () => {
  // Field ID constants (mirroring the const declarations in dashboard.html)
  const F = {
    SP:          'customfield_10006',
    SPRINT:      'customfield_10001',
    POD:         'customfield_12904',
    DEV_OWNER:   'customfield_10125',
    ANALYST_DUE: 'customfield_10304',
    DEV_DUE:     'customfield_10305',
    SEVERITY:    'customfield_10130',
    SC:          'customfield_14249',
    TEAM:        'customfield_16304',
    PRODUCT:     'customfield_10123',
    DOMAIN:      'customfield_17813',
  };
  const BASE = 'https://jira.example.com';

  /** Build a minimal valid Jira issue with optional field overrides. */
  function makeIssue(fieldOverrides = {}) {
    return {
      key: 'BMO-42',
      fields: {
        summary:  'Test story',
        status:   { name: 'Build' },
        issuetype:{ name: 'Story' },
        assignee: { displayName: 'Dev User',      name: 'duser' },
        reporter: { displayName: 'Reporter Name', name: 'rname' },
        [F.SP]:          5,
        [F.SPRINT]:      null,
        [F.DEV_OWNER]:   { displayName: 'Dev Owner', name: 'downer' },
        [F.ANALYST_DUE]: '2026-06-01',
        [F.DEV_DUE]:     '2026-07-01',
        [F.SEVERITY]:    { value: '2 - Major' },
        [F.TEAM]:        { value: 'Core DEV' },
        [F.PRODUCT]:     { value: 'Trading Platform' },
        [F.DOMAIN]:      { value: 'Derivatives' },
        [F.SC]:          { displayName: 'SC Person' },
        issuelinks:      [],
        ...fieldOverrides,
      },
    };
  }

  it('extracts key', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).key).toBe('BMO-42');
  });
  it('extracts summary', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).summary).toBe('Test story');
  });
  it('extracts SP as a number', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).sp).toBe(5);
  });
  it('extracts status name', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).status).toBe('Build');
  });
  it('extracts assignee displayName', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).assignee).toBe('Dev User');
  });
  it('falls back to assignee.name when displayName is absent', () => {
    const issue = makeIssue({ assignee: { name: 'duser' } });
    expect(ctx.normalizeIssue(issue, BASE).assignee).toBe('duser');
  });
  it('returns null for assignee when field is null', () => {
    expect(ctx.normalizeIssue(makeIssue({ assignee: null }), BASE).assignee).toBeNull();
  });
  it('extracts reporter displayName', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).reporter).toBe('Reporter Name');
  });
  it('returns null for reporter when field is absent', () => {
    const issue = makeIssue({ reporter: null });
    expect(ctx.normalizeIssue(issue, BASE).reporter).toBeNull();
  });
  it('extracts devDue date string', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).devDue).toBe('2026-07-01');
  });
  it('extracts analystDue date string', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).analystDue).toBe('2026-06-01');
  });
  it('extracts team from value object', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).team).toBe('Core DEV');
  });
  it('extracts product from value object', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).product).toBe('Trading Platform');
  });
  it('extracts domain from value object', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).domain).toBe('Derivatives');
  });
  it('builds the correct Jira browse URL', () => {
    expect(ctx.normalizeIssue(makeIssue(), BASE).url).toBe('https://jira.example.com/browse/BMO-42');
  });
  it('returns null sp when SP field is null', () => {
    expect(ctx.normalizeIssue(makeIssue({ [F.SP]: null }), BASE).sp).toBeNull();
  });
  it('handles fully missing fields gracefully (no crash)', () => {
    const n = ctx.normalizeIssue({ key: 'BMO-X', fields: {} }, BASE);
    expect(n.key).toBe('BMO-X');
    expect(n.summary).toBe('');
    expect(n.assignee).toBeNull();
    expect(n.sp).toBeNull();
  });
  it('parses sprint field and returns state=active for an active sprint', () => {
    const sprintRaw = ['sprint@[id=95,name=Sprint 95,state=ACTIVE]'];
    const n = ctx.normalizeIssue(makeIssue({ [F.SPRINT]: sprintRaw }), BASE);
    expect(n.sprint).not.toBeNull();
    expect(n.sprint.state).toBe('active');
    expect(n.sprint.id).toBe(95);
  });
  it('returns null sprint when sprint field is null', () => {
    const n = ctx.normalizeIssue(makeIssue({ [F.SPRINT]: null }), BASE);
    expect(n.sprint).toBeNull();
  });
  it('extracts devOwnerName from F_DEV_OWNER field', () => {
    const n = ctx.normalizeIssue(makeIssue(), BASE);
    expect(n.devOwnerName).toBe('Dev Owner');
  });
  it('extracts scName from F_SC field', () => {
    const n = ctx.normalizeIssue(makeIssue(), BASE);
    expect(n.scName).toBe('SC Person');
  });
  it('extracts issueType name', () => {
    const n = ctx.normalizeIssue(makeIssue(), BASE);
    expect(n.issueType).toBe('Story');
  });
  it('extracts severity value', () => {
    const n = ctx.normalizeIssue(makeIssue(), BASE);
    expect(n.severity).toBe('2 - Major');
  });
  it('returns null for severity when field is absent', () => {
    const n = ctx.normalizeIssue(makeIssue({ [F.SEVERITY]: null }), BASE);
    expect(n.severity).toBeNull();
  });
  it('handles team as array of objects', () => {
    const issue = makeIssue({ [F.TEAM]: [{ value: 'Core' }, { value: 'API' }] });
    expect(ctx.normalizeIssue(issue, BASE).team).toBe('Core, API');
  });
  it('exposes issueLinks from the fields', () => {
    const links = [{ type: { inward: 'is blocked by' }, inwardIssue: { key: 'BMO-1' } }];
    const n = ctx.normalizeIssue(makeIssue({ issuelinks: links }), BASE);
    expect(n.issueLinks).toHaveLength(1);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// normalizeDomain — extracts a domain string from a raw Jira field value
// ════════════════════════════════════════════════════════════════════════════
describe('normalizeDomain', () => {
  it('returns null for null', () => {
    expect(ctx.normalizeDomain(null)).toBeNull();
  });
  it('returns null for undefined', () => {
    expect(ctx.normalizeDomain(undefined)).toBeNull();
  });
  it('returns null for an empty string', () => {
    expect(ctx.normalizeDomain('')).toBeNull();
  });
  it('returns the string when given a plain string', () => {
    expect(ctx.normalizeDomain('Trading')).toBe('Trading');
  });
  it('extracts .value from a Jira value-object', () => {
    expect(ctx.normalizeDomain({ value: 'Payments' })).toBe('Payments');
  });
  it('returns null when object has no .value property', () => {
    expect(ctx.normalizeDomain({ id: '10001' })).toBeNull();
  });
});

// ════════════════════════════════════════════════════════════════════════════
// buildDomainInconsistencies — compares story domain to epic domain
// ════════════════════════════════════════════════════════════════════════════
describe('buildDomainInconsistencies', () => {
  // helpers
  const story = (key, domain, epicKey) => ({ key, summary: `Summary of ${key}`, domain, epicKey, devOwnerName: null });
  const epic  = (key, domain) => ({ key, summary: `Summary of ${key}`, domain });

  it('returns empty array when stories is empty', () => {
    expect(ctx.buildDomainInconsistencies([], {})).toEqual([]);
  });
  it('skips stories with no epic link', () => {
    const r = ctx.buildDomainInconsistencies([story('BMO-1', 'Trading', null)], {});
    expect(r).toHaveLength(0);
  });
  it('returns empty array when all story domains match their epic domain', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', 'Trading', 'BMO-E1')],
      { 'BMO-E1': epic('BMO-E1', 'Trading') },
    );
    expect(r).toHaveLength(0);
  });
  it('returns an inconsistency when story domain differs from epic domain', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', 'Trading', 'BMO-E1')],
      { 'BMO-E1': epic('BMO-E1', 'Payments') },
    );
    expect(r).toHaveLength(1);
    expect(r[0].storyKey).toBe('BMO-1');
    expect(r[0].storyDomain).toBe('Trading');
    expect(r[0].epicKey).toBe('BMO-E1');
    expect(r[0].epicDomain).toBe('Payments');
    expect(r[0].epicFound).toBe(true);
  });
  it('always includes rows where the epic key is not found in epicMap', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', 'Trading', 'BMO-E99')],
      {},
    );
    expect(r).toHaveLength(1);
    expect(r[0].epicFound).toBe(false);
    expect(r[0].epicDomain).toBeNull();
    expect(r[0].epicSummary).toBeNull();
  });
  it('includes epic-not-found even when story domain is null', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', null, 'BMO-E99')],
      {},
    );
    expect(r).toHaveLength(1);
    expect(r[0].epicFound).toBe(false);
  });
  it('flags mismatch when story has null domain but epic has a domain', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', null, 'BMO-E1')],
      { 'BMO-E1': epic('BMO-E1', 'Trading') },
    );
    expect(r).toHaveLength(1);
    expect(r[0].storyDomain).toBeNull();
    expect(r[0].epicDomain).toBe('Trading');
  });
  it('flags mismatch when story has a domain but epic domain is null', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', 'Trading', 'BMO-E1')],
      { 'BMO-E1': epic('BMO-E1', null) },
    );
    expect(r).toHaveLength(1);
    expect(r[0].storyDomain).toBe('Trading');
    expect(r[0].epicDomain).toBeNull();
  });
  it('treats both-null domain as a match and skips it', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', null, 'BMO-E1')],
      { 'BMO-E1': epic('BMO-E1', null) },
    );
    expect(r).toHaveLength(0);
  });
  it('returns correct shape: storyKey, storySummary, epicKey, epicSummary, epicFound', () => {
    const r = ctx.buildDomainInconsistencies(
      [story('BMO-1', 'Trading', 'BMO-E1')],
      { 'BMO-E1': epic('BMO-E1', 'Payments') },
    );
    expect(r[0]).toMatchObject({
      storyKey:     'BMO-1',
      storySummary: 'Summary of BMO-1',
      epicKey:      'BMO-E1',
      epicSummary:  'Summary of BMO-E1',
      epicFound:    true,
    });
  });
  it('handles multiple stories and returns only mismatches', () => {
    const epics = {
      'BMO-E1': epic('BMO-E1', 'Trading'),
      'BMO-E2': epic('BMO-E2', 'Payments'),
    };
    const stories = [
      story('BMO-1', 'Trading',  'BMO-E1'),  // match → skip
      story('BMO-2', 'Trading',  'BMO-E2'),  // mismatch → include
      story('BMO-3', 'Payments', null),       // no epic → skip
    ];
    const r = ctx.buildDomainInconsistencies(stories, epics);
    expect(r).toHaveLength(1);
    expect(r[0].storyKey).toBe('BMO-2');
  });
});

// ════════════════════════════════════════════════════════════════════════════
// countBizDays — Mon–Fri day counter, 'from' inclusive, 'to' exclusive
// ════════════════════════════════════════════════════════════════════════════
describe('countBizDays', () => {
  // Use local noon (T12:00:00) so setHours(0,0,0,0) stays on the same
  // calendar day regardless of the host timezone offset (works for UTC±11).
  function d(s) { return new Date(s + 'T12:00:00'); }

  it('returns 0 when from equals to', () => {
    expect(ctx.countBizDays(d('2026-05-04'), d('2026-05-04'))).toBe(0);
  });
  it('returns 1 for adjacent Mon–Tue', () => {
    // Mon 2026-05-04 → Tue 2026-05-05: counts Mon only
    expect(ctx.countBizDays(d('2026-05-04'), d('2026-05-05'))).toBe(1);
  });
  it('counts 5 weekdays across Mon–Sat span', () => {
    // Mon 4 → Sat 9: Mon,Tue,Wed,Thu,Fri = 5
    expect(ctx.countBizDays(d('2026-05-04'), d('2026-05-09'))).toBe(5);
  });
  it('returns 0 for a Sat→Sun span (no weekdays)', () => {
    expect(ctx.countBizDays(d('2026-05-09'), d('2026-05-10'))).toBe(0);
  });
  it('returns 1 for Fri→Mon span (only Fri counted)', () => {
    // Fri 8 → Mon 11: only Fri(8) is a weekday inside the range
    expect(ctx.countBizDays(d('2026-05-08'), d('2026-05-11'))).toBe(1);
  });
  it('counts 10 biz days across two Mon–Fri weeks', () => {
    // Mon May-04 → Mon May-18: 5 + 5 = 10
    expect(ctx.countBizDays(d('2026-05-04'), d('2026-05-18'))).toBe(10);
  });
  it('handles span starting on Saturday', () => {
    // Sat May-09 → Fri May-15: Mon(11),Tue(12),Wed(13),Thu(14) = 4
    expect(ctx.countBizDays(d('2026-05-09'), d('2026-05-15'))).toBe(4);
  });
  it('returns 0 when from is after to', () => {
    // Reversed dates — loop never runs
    expect(ctx.countBizDays(d('2026-05-08'), d('2026-05-04'))).toBe(0);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// calcSprintContext — enriches sprint with biz-day totalDays / remainingDays
// ════════════════════════════════════════════════════════════════════════════
describe('calcSprintContext', () => {
  it('returns null for null input', () => {
    expect(ctx.calcSprintContext(null)).toBeNull();
  });
  it('returns null when sprint has no startDate', () => {
    expect(ctx.calcSprintContext({ endDate: '2026-05-15' })).toBeNull();
  });
  it('returns null when sprint has no endDate', () => {
    expect(ctx.calcSprintContext({ startDate: '2026-05-04' })).toBeNull();
  });
  it('passes all original sprint fields through to result', () => {
    const sprint = { startDate: '2026-01-05', endDate: '2026-01-16', name: 'Sprint 42', state: 'closed', id: 42 };
    const r = ctx.calcSprintContext(sprint);
    expect(r.name).toBe('Sprint 42');
    expect(r.id).toBe(42);
    expect(r.state).toBe('closed');
  });
  it('computes totalDays as biz days from startDate to endDate', () => {
    // Derive expected using the same UTC date parsing the production code uses,
    // so this test is correct regardless of host timezone.
    const sprint = { startDate: '2026-01-05', endDate: '2026-01-16', name: 'S1' };
    const expected = ctx.countBizDays(new Date('2026-01-05'), new Date('2026-01-16'));
    expect(ctx.calcSprintContext(sprint).totalDays).toBe(expected);
    expect(expected).toBeGreaterThan(0); // sanity: at least 1 biz day
  });
  it('returns 0 for remainingDays when sprint ended well in the past', () => {
    const sprint = { startDate: '2024-01-01', endDate: '2024-01-15', name: 'Old' };
    expect(ctx.calcSprintContext(sprint).remainingDays).toBe(0);
  });
  it('returns remainingDays > 0 for a sprint ending in the future', () => {
    // Note: remainingDays = biz days from TODAY to endDate, which will be
    // larger than totalDays (start→end) when the sprint hasn't started yet.
    const sprint = { startDate: '2028-01-03', endDate: '2028-01-14', name: 'Future' };
    const r = ctx.calcSprintContext(sprint);
    expect(r.remainingDays).toBeGreaterThan(0);
  });
  it('remainingDays is never negative', () => {
    const sprint = { startDate: '2020-01-01', endDate: '2020-01-10', name: 'VeryOld' };
    expect(ctx.calcSprintContext(sprint).remainingDays).toBeGreaterThanOrEqual(0);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// jiraIssuesUrl — builds a /issues/?jql= URL, always appending aggregateExpression
// ════════════════════════════════════════════════════════════════════════════
describe('jiraIssuesUrl', () => {
  const BASE = 'https://jira.example.com';
  const JQL  = 'project = BMO AND assignee = alice';

  it('produces a URL containing /issues/?jql=', () => {
    expect(ctx.jiraIssuesUrl(BASE, JQL)).toContain('/issues/?jql=');
  });
  it('URL-encodes the JQL (no raw spaces in output)', () => {
    expect(ctx.jiraIssuesUrl(BASE, JQL)).not.toContain(' ');
  });
  it('appends aggregateExpression to the encoded JQL', () => {
    const url = ctx.jiraIssuesUrl(BASE, JQL);
    expect(url).toContain(encodeURIComponent('aggregateExpression'));
  });
  it('decoded JQL contains "AND issueFunction in aggregateExpression"', () => {
    const url = ctx.jiraIssuesUrl(BASE, JQL);
    const decoded = decodeURIComponent(url.split('jql=')[1]);
    expect(decoded).toContain('AND issueFunction in aggregateExpression');
  });
  it('strips a trailing slash from base before building the URL', () => {
    const url = ctx.jiraIssuesUrl(BASE + '/', JQL);
    expect(url).not.toContain('//issues');
  });
  it('preserves the base URL prefix', () => {
    expect(ctx.jiraIssuesUrl(BASE, JQL)).toMatch(/^https:\/\/jira\.example\.com/);
  });
  it('original JQL is preserved in the decoded output (before the AND aggFn)', () => {
    const url = ctx.jiraIssuesUrl(BASE, JQL);
    const decoded = decodeURIComponent(url.split('jql=')[1]);
    expect(decoded).toContain(JQL);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// committedActiveSprintSP — sum of SP for active-sprint tickets owned by a dev
// ════════════════════════════════════════════════════════════════════════════
describe('committedActiveSprintSP', () => {
  // domainEditState is `var` in dashboard.html, so it IS accessible as
  // ctx.domainEditState.  We mutate the existing object (Object.assign /
  // key deletion) rather than replacing the reference so the function's
  // closure still reads the same binding.
  afterEach(() => {
    Object.keys(ctx.domainEditState).forEach(k => delete ctx.domainEditState[k]);
  });

  it('returns null when domainEditState is empty', () => {
    expect(ctx.committedActiveSprintSP('alice')).toBeNull();
  });
  it('returns 0 when dev has no tickets at all', () => {
    Object.assign(ctx.domainEditState, { 'BMO-1': { devOwnerUser: 'bob', sprint: { state: 'active' }, sp: 5 } });
    expect(ctx.committedActiveSprintSP('alice')).toBe(0);
  });
  it('sums SP for active-sprint tickets owned by the given user', () => {
    Object.assign(ctx.domainEditState, {
      'BMO-1': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: 5 },
      'BMO-2': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: 3 },
    });
    expect(ctx.committedActiveSprintSP('alice')).toBe(8);
  });
  it('ignores future-sprint tickets', () => {
    Object.assign(ctx.domainEditState, {
      'BMO-1': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: 5 },
      'BMO-2': { devOwnerUser: 'alice', sprint: { state: 'future' }, sp: 8 },
    });
    expect(ctx.committedActiveSprintSP('alice')).toBe(5);
  });
  it('ignores tickets with no sprint', () => {
    Object.assign(ctx.domainEditState, {
      'BMO-1': { devOwnerUser: 'alice', sprint: null, sp: 10 },
      'BMO-2': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: 3 },
    });
    expect(ctx.committedActiveSprintSP('alice')).toBe(3);
  });
  it('treats null sp as 0 in the sum', () => {
    Object.assign(ctx.domainEditState, {
      'BMO-1': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: null },
      'BMO-2': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: 4 },
    });
    expect(ctx.committedActiveSprintSP('alice')).toBe(4);
  });
  it('is case-insensitive on username comparison', () => {
    Object.assign(ctx.domainEditState, {
      'BMO-1': { devOwnerUser: 'Alice', sprint: { state: 'active' }, sp: 6 },
    });
    expect(ctx.committedActiveSprintSP('alice')).toBe(6);
  });
  it('does not include tickets owned by other users', () => {
    Object.assign(ctx.domainEditState, {
      'BMO-1': { devOwnerUser: 'bob',   sprint: { state: 'active' }, sp: 10 },
      'BMO-2': { devOwnerUser: 'alice', sprint: { state: 'active' }, sp: 2 },
    });
    expect(ctx.committedActiveSprintSP('alice')).toBe(2);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// computeTeamStats — aggregates per-person stats into team-level metrics
// (TDD: function must be implemented to pass these tests)
// ════════════════════════════════════════════════════════════════════════════
describe('computeTeamStats', () => {
  /** Build a minimal person-stats object with sensible defaults. */
  function member(overrides = {}) {
    return {
      username:                   'dev1',
      sp30d:                      20,
      count30d:                   5,
      daily30d:                   2.0,
      spActiveSprint:             10,
       spRemainingActiveSprint:    8,
      spLastSprint:               18,
      ...overrides,
    };
  }

  // ── Shape ──────────────────────────────────────────────────────────────
  it('returns devCount equal to input array length', () => {
    const r = ctx.computeTeamStats([member({ username: 'a' }), member({ username: 'b' })]);
    expect(r.devCount).toBe(2);
  });
  it('returns devCount 0 for empty input', () => {
    expect(ctx.computeTeamStats([]).devCount).toBe(0);
  });
  it('includes a memberStats array in the result', () => {
    const r = ctx.computeTeamStats([member({ username: 'a' })]);
    expect(Array.isArray(r.memberStats)).toBe(true);
  });

  // ── 30-day velocity ────────────────────────────────────────────────────
  it('sums sp30d across all members into totalSP30d', () => {
    const r = ctx.computeTeamStats([member({ sp30d: 20 }), member({ sp30d: 30 })]);
    expect(r.totalSP30d).toBe(50);
  });
  it('treats null sp30d as 0 in totalSP30d', () => {
    const r = ctx.computeTeamStats([member({ sp30d: null }), member({ sp30d: 15 })]);
    expect(r.totalSP30d).toBe(15);
  });
  it('computes avgSP30d as totalSP30d / devCount', () => {
    const r = ctx.computeTeamStats([member({ sp30d: 20 }), member({ sp30d: 30 })]);
    expect(r.avgSP30d).toBeCloseTo(25);
  });
  it('returns null for avgSP30d when devCount is 0', () => {
    expect(ctx.computeTeamStats([]).avgSP30d).toBeNull();
  });

  // ── Daily velocity ─────────────────────────────────────────────────────
  it('computes avgDaily30d as mean of non-null daily30d values', () => {
    const r = ctx.computeTeamStats([member({ daily30d: 2.0 }), member({ daily30d: 4.0 })]);
    expect(r.avgDaily30d).toBeCloseTo(3.0);
  });
  it('ignores null daily30d values in the average', () => {
    const r = ctx.computeTeamStats([member({ daily30d: 4.0 }), member({ daily30d: null })]);
    expect(r.avgDaily30d).toBeCloseTo(4.0);
  });
  it('returns null for avgDaily30d when all daily30d values are null', () => {
    const r = ctx.computeTeamStats([member({ daily30d: null })]);
    expect(r.avgDaily30d).toBeNull();
  });
  it('returns null for avgDaily30d on empty input', () => {
    expect(ctx.computeTeamStats([]).avgDaily30d).toBeNull();
  });

  // ── Active sprint ──────────────────────────────────────────────────────
  it('sums spActiveSprint into totalBurnedActive', () => {
    const r = ctx.computeTeamStats([member({ spActiveSprint: 5 }), member({ spActiveSprint: 7 })]);
    expect(r.totalBurnedActive).toBe(12);
  });
  it('treats null spActiveSprint as 0 in totalBurnedActive', () => {
    const r = ctx.computeTeamStats([member({ spActiveSprint: null }), member({ spActiveSprint: 6 })]);
    expect(r.totalBurnedActive).toBe(6);
  });
  it('sums spRemainingActiveSprint into totalRemainingActiveSprint', () => {
    const r = ctx.computeTeamStats([member({ spRemainingActiveSprint: 10 }), member({ spRemainingActiveSprint: 8 })]);
    expect(r.totalRemainingActiveSprint).toBe(18);
  });
  it('treats null spRemainingActiveSprint as 0', () => {
    const r = ctx.computeTeamStats([member({ spRemainingActiveSprint: null }), member({ spRemainingActiveSprint: 8 })]);
    expect(r.totalRemainingActiveSprint).toBe(8);
  });
  it('returns 0 totalRemainingActiveSprint for empty input', () => {
    expect(ctx.computeTeamStats([]).totalRemainingActiveSprint).toBe(0);
  });

  // ── Previous sprint ────────────────────────────────────────────────────
  it('sums spLastSprint into totalSpLastSprint', () => {
    const r = ctx.computeTeamStats([member({ spLastSprint: 18 }), member({ spLastSprint: 22 })]);
    expect(r.totalSpLastSprint).toBe(40);
  });
  it('treats null spLastSprint as 0', () => {
    const r = ctx.computeTeamStats([member({ spLastSprint: null }), member({ spLastSprint: 10 })]);
    expect(r.totalSpLastSprint).toBe(10);
  });

  // ── Member list ────────────────────────────────────────────────────────
  it('includes all input members in memberStats', () => {
    const r = ctx.computeTeamStats([member({ username: 'x' }), member({ username: 'y' })]);
    expect(r.memberStats).toHaveLength(2);
  });
  it('sorts memberStats by sp30d descending', () => {
    const m1 = member({ username: 'a', sp30d: 10 });
    const m2 = member({ username: 'b', sp30d: 30 });
    const r  = ctx.computeTeamStats([m1, m2]);
    expect(r.memberStats[0].username).toBe('b');
    expect(r.memberStats[1].username).toBe('a');
  });
  it('does not mutate the input array order', () => {
    const m1 = member({ username: 'a', sp30d: 10 });
    const m2 = member({ username: 'b', sp30d: 30 });
    const input = [m1, m2];
    ctx.computeTeamStats(input);
    expect(input[0].username).toBe('a'); // original order preserved
  });
});
