//! The contract with the API, checked against the API's own schema.
//!
//! Every other test in this crate replaces the server with something written
//! here, which is why two defects reached a person's screen with the suite
//! green:
//!
//! - `GET /me` answers `{"user": …, "organization": …}` and the client decoded
//!   a flat profile, so **every sign-in** ended in `ApiError::Protocol` - "the
//!   NovaBrief service answered something unexpected" - under the password
//!   field;
//! - the client asked for `/meetings/{meeting_id}/detail`, a path the API has
//!   never served, so opening a report answered 404.
//!
//! Both are visible in `contract/openapi.json`, which is generated from the
//! API's routes by `tools/dump_openapi.py` and committed. Two things are
//! checked against it:
//!
//! 1. **every path the client asks for exists**, read out of the client's own
//!    source, and
//! 2. **every method reads what the API promises**, by answering the real
//!    client from a server that serves the schema and nothing else.
//!
//! The second one is deliberately not a table of "this type parses that
//! schema". It was written that way first, and it was useless: naming
//! `CurrentSession` in the test proved that `CurrentSession` parses `/me`,
//! which was never in doubt - the defect was that `profile()` did not use it.
//! A test that restates the fix cannot fail on the bug. So the requests come
//! out of the client itself, over a socket, and the only thing stated here is
//! which method to call.
//!
//! The bodies are built from the schema's **required** fields only, which is
//! all the server promises. A Rust field that is neither `Option` nor required
//! by the schema fails here rather than in front of somebody.
//!
//! What it does not check: that the *values* mean what we think, and that the
//! deployed server runs this code. The snapshot is the repository's API, and a
//! server running an older build can still disagree with it.

use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
};

use serde_json::{json, Value};

use api_client::{ApiClient, ApiError, UploadedPart};

/// The API's schema, as `tools/dump_openapi.py` last wrote it.
const DOCUMENT: &str = include_str!("../contract/openapi.json");

/// The two files that hold every address the desktop knows.
const CLIENT: [&str; 2] = [
    include_str!("../src/lib.rs"),
    include_str!("../src/meetings.rs"),
];

/// Where the routers are mounted. Not in the client's strings, so not in the
/// paths it extracts either.
const PREFIX: &str = "/api/v1/";

fn document() -> Value {
    serde_json::from_str(DOCUMENT).expect("the snapshot is JSON")
}

/// Follow a `$ref`, as many times as it takes.
fn resolve<'a>(document: &'a Value, schema: &'a Value) -> &'a Value {
    match schema.get("$ref").and_then(Value::as_str) {
        None => schema,
        Some(reference) => {
            let mut node = document;
            for step in reference.trim_start_matches("#/").split('/') {
                node = node.get(step).unwrap_or_else(|| {
                    panic!("{reference} points at nothing in the schema");
                });
            }
            resolve(document, node)
        }
    }
}

/// The smallest body the schema permits: required fields, nothing else.
///
/// Deliberately minimal. A generator that filled in every optional field would
/// pass whatever the Rust type asked for, and the question here is the other
/// one - whether the client can read what the server *guarantees* to send.
fn instance(document: &Value, schema: &Value) -> Value {
    let schema = resolve(document, schema);

    if let Some(constant) = schema.get("const") {
        return constant.clone();
    }
    if let Some(first) = schema
        .get("enum")
        .and_then(Value::as_array)
        .and_then(|values| values.first())
    {
        return first.clone();
    }

    // `str | None` becomes `anyOf: [string, null]`. Null would satisfy every
    // `Option` and prove nothing, so the meaningful variant is chosen.
    for key in ["anyOf", "oneOf", "allOf"] {
        if let Some(variants) = schema.get(key).and_then(Value::as_array) {
            let chosen = variants
                .iter()
                .find(|variant| {
                    resolve(document, variant)
                        .get("type")
                        .and_then(Value::as_str)
                        != Some("null")
                })
                .or_else(|| variants.first())
                .expect("an empty anyOf would be a broken schema");
            return instance(document, chosen);
        }
    }

    match schema.get("type").and_then(Value::as_str) {
        Some("string") => json!("x"),
        Some("integer") => json!(1),
        Some("number") => json!(1.0),
        Some("boolean") => json!(true),
        Some("null") => Value::Null,
        // One element, never zero: an empty list would leave the element type
        // - `UploadSlot`, `TranscriptSegment` - unchecked.
        Some("array") => match schema.get("items") {
            Some(items) => json!([instance(document, items)]),
            None => json!([]),
        },
        _ => {
            let properties = schema.get("properties");
            let required = schema
                .get("required")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let mut object = serde_json::Map::new();
            for name in required.iter().filter_map(Value::as_str) {
                let property = properties
                    .and_then(|all| all.get(name))
                    .unwrap_or_else(|| panic!("{name} is required and not described"));
                object.insert(name.to_owned(), instance(document, property));
            }
            Value::Object(object)
        }
    }
}

/// The body of the successful answer to one operation, as the schema has it.
fn success_body(document: &Value, method: &str, path: &str) -> Value {
    let operation = document
        .get("paths")
        .and_then(|paths| paths.get(path))
        .and_then(|item| item.get(method))
        .unwrap_or_else(|| panic!("{} {path} is not in the schema", method.to_uppercase()));

    // Whatever 2xx it answers with. `finalize` returns 202 - the work is
    // queued, not done - and hard-coding 200 and 201 here would have made this
    // test complain about a route that is perfectly well behaved.
    let responses = operation
        .get("responses")
        .and_then(Value::as_object)
        .expect("an operation answers");
    let mut codes: Vec<&String> = responses
        .keys()
        .filter(|code| code.starts_with('2'))
        .collect();
    codes.sort();

    let schema = codes
        .into_iter()
        .find_map(|code| {
            responses
                .get(code)?
                .get("content")?
                .get("application/json")?
                .get("schema")
        })
        .unwrap_or_else(|| panic!("{} {path} returns no JSON", method.to_uppercase()));

    instance(document, schema)
}

/// Every address the client builds, taken from its source.
///
/// Read rather than called: the paths are scattered across methods that all
/// need a token and a server, and the question - does this address exist - is
/// answerable without either.
fn paths_called() -> Vec<String> {
    const MARKERS: [&str; 4] = ["endpoint(", "post(", "authorised_get(", "authorised_post("];

    let mut found = Vec::new();
    for source in CLIENT {
        for marker in MARKERS {
            for piece in source.split(marker).skip(1) {
                let rest = piece.trim_start();
                // `format!("meetings/{meeting_id}/report")` and `"me"` both
                // count; `self.http.post(url)` does not.
                let rest = rest.strip_prefix("&format!(").unwrap_or(rest);
                let Some(literal) = rest.strip_prefix('"') else {
                    continue;
                };
                let Some(text) = literal.split('"').next() else {
                    continue;
                };
                found.push(text.to_owned());
            }
        }
    }
    found.sort();
    found.dedup();
    found
}

/// No address the client uses is missing from the API.
#[test]
fn every_path_the_client_asks_for_exists() {
    let document = document();
    let called = paths_called();

    // The parser going quiet would turn this test into a green light for
    // nothing at all.
    assert!(
        called.len() >= 6,
        "only found {called:?}; the parser has stopped reading the client"
    );

    let paths = document.get("paths").expect("the schema lists paths");
    let missing: Vec<String> = called
        .iter()
        .map(|path| format!("{PREFIX}{path}"))
        .filter(|full| paths.get(full).is_none())
        .collect();

    assert!(
        missing.is_empty(),
        "the client calls addresses the API does not serve: {missing:#?}\n\
         served: {:#?}",
        paths
            .as_object()
            .map(|all| all.keys().cloned().collect::<Vec<_>>())
            .unwrap_or_default()
    );
}

/// A server that answers exactly what the schema promises, and nothing else.
///
/// The client's own loopback exception makes this possible: plain HTTP is
/// refused everywhere except `127.0.0.1`, precisely so a test can be a real
/// socket rather than a mock.
///
/// An address it has no operation for is answered 404, which is what a wrong
/// path deserves and what makes the failure legible.
fn serve(listener: TcpListener, document: Value, requests: usize) {
    for _ in 0..requests {
        let Ok((mut stream, _)) = listener.accept() else {
            return;
        };
        let mut reader = BufReader::new(stream.try_clone().expect("the socket clones"));

        let mut head = String::new();
        let mut length = 0usize;
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                break;
            }
            let lowered = line.to_ascii_lowercase();
            if let Some(value) = lowered.strip_prefix("content-length:") {
                length = value.trim().parse().unwrap_or(0);
            }
            head.push_str(&line);
        }
        // Read the body even though nothing looks at it: an unread body on a
        // closing socket reaches the client as a reset connection, and the
        // test would fail for a reason that has nothing to do with contracts.
        if length > 0 {
            let mut body = vec![0; length];
            let _ = reader.read_exact(&mut body);
        }

        let mut first = head.split_whitespace();
        let method = first.next().unwrap_or_default().to_ascii_lowercase();
        let path = first.next().unwrap_or_default().to_owned();

        let answer = match promised_body(&document, &method, &path) {
            Some(body) => format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
                 content-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            ),
            None => "HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
                .to_owned(),
        };
        let _ = stream.write_all(answer.as_bytes());
        let _ = stream.flush();
    }
}

/// The body the schema promises for a concrete request, if it describes one.
fn promised_body(document: &Value, method: &str, path: &str) -> Option<String> {
    let paths = document.get("paths")?.as_object()?;
    let wanted: Vec<&str> = path.split('/').collect();

    let template = paths.keys().find(|candidate| {
        let parts: Vec<&str> = candidate.split('/').collect();
        parts.len() == wanted.len()
            && parts
                .iter()
                .zip(&wanted)
                // `{meeting_id}` stands for whatever identifier was sent.
                .all(|(part, sent)| part.starts_with('{') || part == sent)
    })?;

    paths.get(template)?.get(method)?;
    Some(success_body(document, method, template).to_string())
}

/// "It worked", or why not.
///
/// The values are not the subject here - whether the client could read them
/// at all is.
fn read<T>(outcome: Result<T, ApiError>) -> Result<(), String> {
    outcome.map(drop).map_err(|error| error.to_string())
}

/// Every method of the client reads what the API says it will send.
///
/// The calls are real: a socket, a request, an answer built from the schema.
/// Whatever the client does between them - which path it builds, which type it
/// decodes into, which statuses it accepts - is under test rather than
/// restated.
#[tokio::test]
async fn every_method_reads_what_the_api_promises() {
    let document = document();
    let listener = TcpListener::bind("127.0.0.1:0").expect("a free port");
    let address = format!("http://{}", listener.local_addr().expect("an address"));

    // One request per call below. The server stops on its own afterwards.
    let server = std::thread::spawn(move || serve(listener, document, 9));

    let client = ApiClient::new(&address).expect("loopback HTTP is allowed on purpose");
    let meeting = "01a09ac1-e36e-7b61-9ad4-fb4a2c7bc4d9";
    let parts = [UploadedPart {
        part_number: 1,
        etag: "\"abc\"".to_owned(),
    }];

    let mut broken = Vec::new();
    let mut check = |name: &str, outcome: Result<(), String>| {
        if let Err(error) = outcome {
            broken.push(format!("{name}: {error}"));
        }
    };

    check("sign_in", read(client.sign_in("a@b.co", "secret").await));
    check("refresh", read(client.refresh("token").await));
    // The one that shipped broken: 200, and unreadable.
    check("profile", read(client.profile("token").await));
    check(
        "declare_meeting",
        read(
            client
                .declare_meeting("token", "2026-09-13T09:00:00Z", None)
                .await,
        ),
    );
    check("meetings", read(client.meetings("token").await));
    check(
        "start_upload",
        read(
            client
                .start_upload("token", meeting, 1024, "abc", 60, 0)
                .await,
        ),
    );
    check(
        "resume_upload",
        read(client.resume_upload("token", meeting).await),
    );
    check(
        "finalize",
        read(client.finalize("token", meeting, "upload", &parts).await),
    );
    // The other one: a path the API has never served.
    check(
        "meeting_detail",
        read(client.meeting_detail("token", meeting).await),
    );

    let _ = server.join();

    assert!(
        broken.is_empty(),
        "these calls fail against the API's own schema:\n  {}\n\n\
         \"answered something unexpected\" means the body did not fit the type; \
         \"answered 404\" means the path does not exist.",
        broken.join("\n  ")
    );
}
