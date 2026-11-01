"""Review probe (NOT part of the patch): check the payload against the LOCAL official OpenAPI spec. No network, no request."""
import sys, os, json, yaml
sys.path.insert(0, "/tmp/pchk2"); os.environ.setdefault("SESSION_SECRET_KEY", "x"*40)
import reddit_live as rl
SPEC = "/home/user/openai/openai-openapi/openapi.yaml"
doc = yaml.load(open(SPEC), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
S = doc["components"]["schemas"]
print("spec info:", doc["info"].get("title"), doc["info"].get("version"), "| schemas:", len(S))

def resolve(node):
    while isinstance(node, dict) and "$ref" in node:
        node = S[node["$ref"].split("/")[-1]]
    return node
def props(node, seen=None):
    """All property schemas of an (allOf-composed) object schema."""
    seen = seen or set(); out = {}
    node = resolve(node)
    for sub in node.get("allOf", []): out.update(props(sub, seen))
    out.update(node.get("properties", {}))
    return out
def required(node):
    node = resolve(node); r = set(node.get("required", []))
    for sub in node.get("allOf", []): r |= required(sub)
    return r

p = rl.build_payload("find me a reddit monitoring tool")
print("payload keys:", sorted(p))
cr = S["CreateResponse"]; cp = props(cr)
print("CreateResponse required:", sorted(required(cr)))
print("payload keys NOT in CreateResponse properties:", [k for k in p if k not in cp])
print("CreateResponse required but missing from payload:", sorted(required(cr) - set(p)))

# jsonschema validation of the whole payload against CreateResponse (OpenAPI 3.1 == JSON Schema 2020-12)
import jsonschema
root = {"$ref": "#/components/schemas/CreateResponse", "components": {"schemas": S}}
v = jsonschema.Draft202012Validator(root)
errs = sorted(v.iter_errors(p), key=lambda e: list(e.path))
print("jsonschema errors against CreateResponse:", len(errs))
for e in errs[:6]: print("  ", list(e.path), e.message[:160])

# tool object
tools = cp["tools"]; print("tools schema keys:", list(resolve(tools).keys())[:6])
# find the web_search tool schema by its properties
cands = [n for n, sc in S.items() if isinstance(sc, dict) and "external_web_access" in props(sc) and "filters" in props(sc)]
print("web_search tool schema(s):", cands)
for n in cands[:1]:
    sc = S[n]; pr = props(sc)
    print("  type enum:", resolve(pr["type"]).get("enum"), "| props:", sorted(pr))
    af = json.dumps(pr["filters"])[:300]; print("  filters:", af)
    print("  our tool validates:", all(True for _ in [0]) and jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{n}", "components": {"schemas": S}}).is_valid(p["tools"][0]))
# enumerations
print("tool_choice schema:", json.dumps(cp["tool_choice"])[:260])
tco = S.get("ToolChoiceOptions", {}); print("ToolChoiceOptions enum:", tco.get("enum"))
inc = S.get("IncludeEnum", {}); print("IncludeEnum has web_search_call.action.sources:", "web_search_call.action.sources" in (inc.get("enum") or []))
rs = props(S["Reasoning"]) if "Reasoning" in S else {}; print("Reasoning props:", sorted(rs))
eff = resolve(rs.get("effort", {})) if rs else {}; print("reasoning.effort schema:", json.dumps(eff)[:300])
for k in ("model","instructions","input","max_output_tokens","max_tool_calls","store"):
    print(f"  {k}:", json.dumps(cp.get(k))[:200])
# output shapes the parser reads
for n in ("WebSearchActionSearch","UrlCitationBody","ResponseUsage"):
    sc = S.get(n); print(n, "->", sorted(props(sc)) if sc else "NOT FOUND")
src = props(S["WebSearchActionSearch"])["sources"]; print("WebSearchActionSearch.sources item:", json.dumps(src)[:420])
