//! Cooperative update exit. Fixed local files carry no executable commands.
use std::{fs, io::Write, path::{Path,PathBuf}, time::{SystemTime,UNIX_EPOCH}};
use std::os::windows::fs::MetadataExt;
use serde_json::Value;

pub struct StopRequest { pub token:String, pub launch_id:String }

pub fn local_data(program:&Path)->PathBuf {
    std::env::var_os("KNOWLEDGE_BRIDGE_LOCAL_DATA").filter(|v|!v.is_empty())
        .map(PathBuf::from).unwrap_or_else(||program.parent().unwrap().join("control"))
}
fn clean_path(path:&Path)->bool {
    path.ancestors().all(|p|fs::symlink_metadata(p).map(|m|m.file_attributes()&0x400==0).unwrap_or(false))
}
fn same_path(value:&Value,expected:&Path)->bool {
    let Some(value)=value.as_str() else{return false};
    match (fs::canonicalize(value),fs::canonicalize(expected)) {
        (Ok(a),Ok(b))=>a.to_string_lossy().eq_ignore_ascii_case(&b.to_string_lossy()),_=>false
    }
}
fn valid_token(value:&str)->bool {
    (16..=80).contains(&value.len()) && value.bytes().all(|v|v.is_ascii_alphanumeric()||v==b'-')
}
pub fn validate(value:&Value,launch_id:&str,program:&Path,local:&Path,now:f64)->Option<StopRequest> {
    let token=value["token"].as_str()?;
    let created=value["created_at_epoch"].as_f64()?;
    let expires=value["expires_at_epoch"].as_f64()?;
    let roles=value["roles"].as_array()?;
    if value["schema"].as_u64()!=Some(1) || value["format"].as_str()!=Some("jiedu-update-stop-v1")
        || value["launch_id"].as_str()!=Some(launch_id) || !valid_token(launch_id) || !valid_token(token)
        || !created.is_finite() || !expires.is_finite() || !now.is_finite()
        || created>now+30. || expires<=now || expires<=created || expires-created>600.5
        || !roles.iter().any(|v|v.as_str()==Some("native"))
        || !roles.iter().all(|v|matches!(v.as_str(),Some("console"|"watch"|"native")))
        || !same_path(&value["bridge_dir"],program) || !same_path(&value["local_data"],local) {return None;}
    Some(StopRequest{token:token.into(),launch_id:launch_id.into()})
}
fn read_json(path:&Path,limit:u64)->Option<Value> {
    if !clean_path(path) {return None;}
    let metadata=fs::metadata(path).ok()?;
    if !metadata.is_file() || metadata.len()>limit {return None;}
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}
pub fn pending(program:&Path)->Option<StopRequest> {
    let local=local_data(program);
    let state=read_json(&local.join("runtime/bridge-runtime.json"),65536)?;
    if !same_path(&state["bridge_dir"],program) {return None;}
    let launch_id=state["launch_id"].as_str()?;
    let request=read_json(&local.join("runtime/update-stop.json"),8192)?;
    let now=SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs_f64();
    validate(&request,launch_id,program,&local,now)
}
pub fn acknowledge(program:&Path,request:&StopRequest)->std::io::Result<()> {
    let runtime=local_data(program).join("runtime");
    if !clean_path(&runtime) {return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied,"Invalid update runtime directory"));}
    let path=runtime.join(format!("update-stop-{}-native.json",request.token));
    if path.exists(){return Ok(());}
    let temporary=runtime.join(format!(".update-stop-{}-native-{}.tmp",request.token,std::process::id()));
    let data=serde_json::to_vec_pretty(&serde_json::json!({"token":request.token,"launch_id":request.launch_id,
        "role":"native","pid":std::process::id(),"complete":true,"exit_requested":true,
        "actual_process_exit_must_be_waited":true})).map_err(|error|std::io::Error::new(std::io::ErrorKind::InvalidData,error))?;
    let mut file=fs::OpenOptions::new().write(true).create_new(true).open(&temporary)?;
    file.write_all(&data)?;file.sync_all()?;drop(file);
    // Each launch has a unique token. Never overwrite a pre-existing ack/link.
    fs::rename(&temporary,&path)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accepts_only_scope_bound_current_short_lived_native_requests(){
        let root=std::env::temp_dir().join(format!("jiedu-stop-test-{}",std::process::id()));
        let program=root.join("program");let local=root.join("control");
        fs::create_dir_all(&program).unwrap();fs::create_dir_all(&local).unwrap();
        let launch="0123456789abcdef0123456789abcdef";
        let value=serde_json::json!({"schema":1,"format":"jiedu-update-stop-v1","token":launch,"launch_id":launch,
            "roles":["console","watch","native"],"bridge_dir":program,"local_data":local,
            "created_at_epoch":1000.,"expires_at_epoch":1600.});
        assert!(validate(&value,launch,&program,&local,1100.).is_some());
        for (field,bad) in [("token",serde_json::json!("../not-a-token")),("launch_id",serde_json::json!("another-launch-0000")),
            ("schema",serde_json::json!(2)),("roles",serde_json::json!(["console"])),("expires_at_epoch",serde_json::json!(1099.)),
            ("created_at_epoch",serde_json::json!(1200.)),("bridge_dir",serde_json::json!(local)),("local_data",serde_json::json!(program))] {
            let mut changed=value.clone();changed[field]=bad;
            assert!(validate(&changed,launch,&program,&local,1100.).is_none(),"{field}");
        }
        assert!(validate(&value,"new-launch-00000000",&program,&local,1100.).is_none());
    }
}
