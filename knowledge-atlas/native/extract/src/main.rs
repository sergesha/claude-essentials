use std::io::Read;
fn prepare(input: &serde_json::Value) -> serde_json::Value {
    let messages = utopia_extract::open::build_open_messages(
        input["filename"].as_str().expect("filename required"), &[], None,
        input["text"].as_str().expect("text required"));
    serde_json::to_value(messages).expect("messages serialize")
}
fn parse(raw: &str) -> Result<serde_json::Value, String> {
    let x = utopia_extract::open::parse_open_response(raw).map_err(|e| e.to_string())?;
    Ok(serde_json::json!({
        "entities": x.entities.iter().map(|e| serde_json::json!({"name":e.name,"kind":e.kind,"named":e.named})).collect::<Vec<_>>(),
        "statements": x.statements.iter().map(|s| serde_json::json!({
            "subject":s.subject,"phrase":s.phrase,"object":s.object,"value":s.value,
            "qualifiers":s.qualifiers,"when":s.when,"ended":s.ended,"quote":s.quote
        })).collect::<Vec<_>>(),
        "names": x.names.iter().map(|n| serde_json::json!({"entity":n.entity,"name":n.name,"quote":n.quote})).collect::<Vec<_>>(),
        "time": x.time, "skipped":x.skipped,"truncated":x.truncated
    }))
}
fn native(mode: &str, input: &serde_json::Value) -> Result<serde_json::Value, String> {
    use utopia_extract::{phrase_align as pa, ontology_agent as oa};
    let rows=input["items"].as_array().ok_or("items required")?;
    let catalog=input["catalog"].as_array().ok_or("catalog required")?;
    let string = |v: &serde_json::Value| v.as_str().unwrap_or("").to_owned();
    let examples: Vec<Vec<String>>=rows.iter().map(|r| r["examples"].as_array().map(|a|a.iter().map(string).collect()).unwrap_or_default()).collect();
    let quotes: Vec<Vec<String>>=rows.iter().map(|r| r["quotes"].as_array().map(|a|a.iter().map(string).collect()).unwrap_or_default()).collect();
    let descriptions: Vec<String>=catalog.iter().map(|c|format!("{} Subject: {}. Object: {}.",c["definition"].as_str().unwrap_or(""),c["subject_role"].as_str().unwrap_or(""),c["object_role"].as_str().unwrap_or(""))).collect();
    let candidates = |object_is_value: bool, selected: Option<&Vec<serde_json::Value>>| catalog.iter().zip(&descriptions)
        .filter(|(c,_)| c["kind"].as_str().unwrap_or("relation") == if object_is_value {"attribute"} else {"relation"})
        .filter(|(c,_)| selected.is_none_or(|ids| ids.iter().any(|id| id.as_str() == c["id"].as_str())))
        .map(|(c,d)|pa::PropertyCandidate {
        key:c["id"].as_str().unwrap(),label:c["id"].as_str().unwrap(),description:d,
        kind:c["kind"].as_str().unwrap_or("relation"),temporal:c["temporal"].as_str().unwrap_or("unspecified"),domains:vec![],ranges:vec![],via:vec![]
    }).collect();
    let items:Vec<_>=rows.iter().enumerate().map(|(i,r)|pa::PhraseItem {
        id:r["id"].as_i64().unwrap(),phrase:r["phrase"].as_str().unwrap_or(""),subject_class:None,object_class:None,
        object_is_value:r["object_is_value"].as_bool().unwrap_or(false),statement_count:1,
        examples:&examples[i],quotes:&quotes[i],candidates:candidates(r["object_is_value"].as_bool().unwrap_or(false),r["candidate_ids"].as_array()),also_allowed:vec![]
    }).collect();
    match mode {
        "phrase_prepare"=>Ok(serde_json::to_value(pa::build_phrase_messages(&items)).unwrap()),
        "phrase_parse"=> {
            let (choices,malformed)=pa::parse_phrase_response(input["response"].as_str().ok_or("response required")?,&items).map_err(|e|e.to_string())?;
            Ok(serde_json::json!({"choices":choices.iter().map(|c|serde_json::json!({"id":c.id,"key":c.property.as_ref().map(|p|&p.0),"direction":c.property.as_ref().map(|p|p.1.as_str()),"marks":c.marks.map(|m|m.as_str())})).collect::<Vec<_>>(),"malformed":malformed}))
        },
        "proposal_prepare"=> {
            let signatures:Vec<_>=items.iter().map(|i|oa::OpenSignature{id:i.id,phrase:i.phrase,subject_class:None,object_class:None,object_is_value:i.object_is_value,statement_count:1,examples:i.examples,quotes:i.quotes}).collect();
            let glossary=oa::Glossary{classes:vec![],others:vec![],properties:catalog.iter().zip(&descriptions).map(|(c,d)|(c["id"].as_str().unwrap(),c["id"].as_str().unwrap(),c["kind"].as_str().unwrap_or("relation"),vec![],vec![],d.as_str())).collect()};
            Ok(serde_json::to_value(oa::build_proposal_messages(&signatures,&[],&glossary,&[])).unwrap())
        },
        "proposal_parse"=> {
            let ids=items.iter().map(|i|i.id).collect();
            let reply=oa::parse_proposal_response(input["response"].as_str().ok_or("response required")?,&ids,&Default::default(),&Default::default()).map_err(|e|e.to_string())?;
            Ok(serde_json::json!({"proposals":reply.proposals.iter().map(|p|serde_json::json!({"kind":p.kind,"key":p.key,"label":p.label,"definition":p.definition,"value":p.value,"datatype":p.datatype,"domains":p.domains,"ranges":p.ranges,"signatures":p.signatures})).collect::<Vec<_>>(),"existing":reply.existing.iter().map(|e|serde_json::json!({"key":e.key,"direction":e.direction,"signatures":e.signatures})).collect::<Vec<_>>(),"malformed":reply.malformed,"reasons":reply.reasons}))
        },
        _=>Err("unknown native mode".into())
    }
}

fn main() {
    let mut raw = String::new();
    std::io::stdin().read_to_string(&mut raw).unwrap();
    let mode = std::env::args().nth(1).unwrap_or("prepare".into());
    let out = match mode.as_str() {
        "prepare" => prepare(&serde_json::from_str(&raw).expect("input JSON required")),
        "parse" => parse(&raw).expect("invalid extraction response"),
        _ => native(&mode, &serde_json::from_str(&raw).expect("input JSON required")).expect("invalid native request"),
    };
    println!("{}", out);
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_alignment_rejects_unknown_keys_and_preserves_direction() {
        let input=serde_json::json!({"items":[{"id":0,"phrase":"is supported by","examples":["Claim is supported by Report"],"quotes":["Report supports Claim"]}],"catalog":[{"id":"supports","definition":"Evidence supports claim","subject_role":"evidence","object_role":"claim"}],"response":"{\"b\":[[0,\"supports\",\"reverse\"]]}"});
        let parsed=native("phrase_parse", &input).unwrap();
        assert_eq!(parsed["choices"][0]["direction"], "reverse");
        let mut unknown=input.clone();unknown["response"]=serde_json::json!("{\"b\":[[0,\"invented\",\"forward\"]]}");
        assert_eq!(native("phrase_parse", &unknown).unwrap()["malformed"],1);
        assert!(native("phrase_prepare", &input).unwrap()[1]["content"].as_str().unwrap().contains("Evidence supports claim"));
    }
    #[test]
    fn parsed_relations_keep_quotes_unknown_time_and_malformed_item_count() {
        let raw = r#"{"e":[["P-31","trial",1]],"s":[["P-31 failed; cause unknown.","P-31","outcome",null,"failed",{"cause":"unknown"},null,null],17],"n":[],"t":[]}"#;
        let out = parse(raw).unwrap();
        assert_eq!(out["statements"][0]["quote"], "P-31 failed; cause unknown.");
        assert_eq!(out["statements"][0]["when"], serde_json::Value::Null);
        assert_eq!(out["skipped"], 1);
        assert_eq!(out["truncated"], false);
        assert!(parse("not JSON").is_err());
    }
    #[test]
    fn prepares_the_actual_wiki_text_instead_of_a_fixed_qualification_fixture() {
        let value = prepare(&serde_json::json!({"filename":"wiki.md","text":"P-14 needs dried feedstock; cause of P-31 unknown."}));
        assert!(value.is_array(), "native extractor messages required");
        let text = value.to_string();
        assert!(text.contains("P-14 needs dried feedstock"));
        assert!(text.contains("cause of P-31 unknown"));
        assert!(text.contains("wiki.md"));
    }
}
