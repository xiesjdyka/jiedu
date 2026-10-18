#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{path::{Path,PathBuf},process::{Command,Stdio},sync::{Mutex,atomic::{AtomicBool,Ordering}}};
use std::os::windows::process::CommandExt;
use tauri::{Manager,WebviewWindowBuilder,WebviewUrl,menu::MenuBuilder,tray::{TrayIconBuilder,TrayIconEvent,MouseButton,MouseButtonState}};
use tauri_plugin_dialog::{DialogExt,MessageDialogKind};
mod update_stop;

const APP_USER_MODEL_ID:&str="local.jiedu.desktop";
#[link(name="shell32")]
extern "system" {fn SetCurrentProcessExplicitAppUserModelID(app_id:*const u16)->i32;}
fn set_windows_app_identity(){
    let value:Vec<u16>=APP_USER_MODEL_ID.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {SetCurrentProcessExplicitAppUserModelID(value.as_ptr());}
}
fn exit_for_update(app:&tauri::AppHandle,program:&Path)->bool {
    if let Some(request)=update_stop::pending(program){
        let requested=app.try_state::<Acceptance>().map(|s|s.update_exit.swap(true,Ordering::SeqCst)).unwrap_or(false);
        if !requested {let _=update_stop::acknowledge(program,&request);app.exit(0);}
        return true;
    }
    false
}

const NATIVE_BUILD_VERSION: &str = env!("JIEDU_RELEASE_VERSION");
fn about_message(product: Option<&str>) -> String {
    format!("界渡程序版 {}\n桌面外壳构建 {NATIVE_BUILD_VERSION}\n知识库、归档与模型保存在你选择的工作区。\n点 × 收起到右下角托盘，单击托盘图标重新打开。\n右键托盘可检查软件更新；选择“退出并停止后台”才会完全退出。\n更新会校验签名，并保留资料和进度。",product.unwrap_or("版本未确认"))
}
fn tray_tooltip(product: Option<&str>, state: &str) -> String {format!("界渡 {} · {state}",product.unwrap_or("版本未确认"))}
fn version_report(product: Option<&str>, error: Option<&str>) -> serde_json::Value {
    let mut result=serde_json::json!({"product_version":product,"product_version_verified":product.is_some(),
        "native_build_version":NATIVE_BUILD_VERSION,"package_version":env!("CARGO_PKG_VERSION"),
        "windows_file_version":env!("JIEDU_WINDOWS_FILE_VERSION"),"about_message":about_message(product),
        "tray_tooltip":tray_tooltip(product,"后台服务已连接 · 单击打开"),"version_source":"program metadata validated by knowledge_bridge.release_version; native version compiled from VERSION",
        "brand":"v1-door","window_icon_size":[32,32],"tray_icon_size":[16,16],"app_user_model_id":APP_USER_MODEL_ID,
        "cooperative_update_stop_format":"jiedu-update-stop-v1"});
    if let Some(error)=error {result["version_error"]=serde_json::Value::String(error.into());}
    result
}
type VersionStamp=(Option<(std::time::SystemTime,u64)>,Option<(std::time::SystemTime,u64)>);
struct VersionState {stamp:VersionStamp,product:Option<String>,error:Option<String>}
fn version_stamp(program:&Path)->VersionStamp {
    let stamp=|name:&str| std::fs::metadata(program.join(name)).ok().and_then(|m|m.modified().ok().map(|t|(t,m.len())));
    (stamp("VERSION"),stamp("pyproject.toml"))
}
fn read_product_version(program:&Path)->Result<String,String> {
    let output=run_python(program,"knowledge_bridge.release_version",&[])?;
    let value:serde_json::Value=serde_json::from_str(&output).map_err(|_|"程序版本信息无效".to_string())?;
    value["product_version"].as_str().filter(|s|!s.is_empty()).map(String::from).ok_or_else(||"程序版本未确认".into())
}
fn load_version(program:&Path)->VersionState {
    let stamp=version_stamp(program);
    match read_product_version(program) {Ok(product)=>VersionState{stamp,product:Some(product),error:None},Err(error)=>VersionState{stamp,product:None,error:Some(error)}}
}
fn current_tooltip(app:&tauri::AppHandle)->String {
    let version=app.state::<Mutex<VersionState>>();let version=version.lock().unwrap();
    tray_tooltip(version.product.as_deref(),if app.state::<Acceptance>().connected.load(Ordering::SeqCst){"后台服务已连接 · 单击打开"}else{"正在连接工作区"})
}
fn refresh_version(app:&tauri::AppHandle) {
    let program=app.state::<Mutex<PathBuf>>().lock().unwrap().clone();
    let state=app.state::<Mutex<VersionState>>();let mut state=state.lock().unwrap();
    if state.stamp==version_stamp(&program){return;}
    *state=load_version(&program);drop(state);
    if let Some(tray)=app.tray_by_id("jiedu"){let _=tray.set_tooltip(Some(current_tooltip(app)));}
}

fn program_path() -> Option<PathBuf> {
    let mut args=std::env::args().skip(1);
    while let Some(arg)=args.next() { if arg=="--program" { return args.next().map(PathBuf::from); } }
    let exe=std::env::current_exe().ok()?;
    for parent in exe.ancestors().skip(1) {
        for candidate in [parent.join("program"),parent.join(".jiedu/program")] {
            if candidate.join("knowledge_bridge/desktop_launcher.py").is_file() {return Some(candidate)}
        }
    }
    None
}
fn python(program:&Path)->PathBuf {program.parent().unwrap().join("runtime/python/python.exe")}
fn run_python(program:&Path,module:&str,args:&[&str])->Result<String,String> {
    let output=Command::new(python(program)).arg("-B").arg("-X").arg("utf8").arg("-m").arg(module).args(args)
        .current_dir(program).creation_flags(0x08000000).stdin(Stdio::null()).output().map_err(|e|e.to_string())?;
    if !output.status.success(){return Err(String::from_utf8_lossy(&output.stderr).chars().take(900).collect())}
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}
fn port(program:&Path)->u16 {
    let p=update_stop::local_data(program).join("bridge-settings.json");
    std::fs::read(p).ok().and_then(|b|serde_json::from_slice::<serde_json::Value>(&b).ok())
        .and_then(|v|v["console"]["port"].as_u64()).unwrap_or(8765) as u16
}
#[derive(Default)]
struct Acceptance { loaded: AtomicBool, close_received: AtomicBool, connected: AtomicBool, window_icon_set:AtomicBool, update_exit:AtomicBool }
fn show_main(app:&tauri::AppHandle) {
    let program=app.state::<Mutex<PathBuf>>().lock().unwrap().clone();
    if exit_for_update(app,&program){return;}
    if let Some(w)=app.get_webview_window("main"){
        if app.state::<Acceptance>().connected.load(Ordering::SeqCst) && w.url().map(|u|u.host_str()!=Some("127.0.0.1")).unwrap_or(true) {
            let program=app.state::<Mutex<PathBuf>>().lock().unwrap().clone();
            let _=w.navigate(format!("http://127.0.0.1:{}/",port(&program)).parse().unwrap());
        }
        let _=w.show();let _=w.unminimize();let _=w.set_focus();
    }
}
fn main(){
    let args:Vec<String>=std::env::args().collect();
    if let Some(index)=args.iter().position(|a|a=="--update-stop-probe") {
        // A read-only acceptance probe never creates a window, service or ack.
        let program=args.windows(2).find(|p|p[0]=="--program").map(|p|PathBuf::from(&p[1]));
        let accepted=program.as_ref().and_then(|p|update_stop::pending(p)).is_some();
        let report=serde_json::to_vec_pretty(&serde_json::json!({"accepted":accepted,"read_only":true,
            "window_created":false,"service_started":false,"exit_or_ack_requested":false})).unwrap();
        let path=args.get(index+1).filter(|p|!p.starts_with("--")).expect("Probe output required");
        std::fs::write(path,report).expect("Could not write probe report");return;
    }
    if let Some(index)=args.iter().position(|a|a=="--version-report") {
        // A build-only version probe must not discover an unrelated ancestor workspace.
        let explicit_program=args.windows(2).find(|p|p[0]=="--program").map(|p|PathBuf::from(&p[1]));
        let state=explicit_program.map(|program|load_version(&program));
        let report=serde_json::to_vec_pretty(&version_report(state.as_ref().and_then(|s|s.product.as_deref()),state.as_ref().and_then(|s|s.error.as_deref()))).unwrap();
        if let Some(path)=args.get(index+1).filter(|p|!p.starts_with("--")) {
            std::fs::write(path,report).expect("Could not write version report");
        } else {println!("{}",String::from_utf8(report).unwrap());}
        return;
    }
    let acceptance=args.windows(2).find(|p|p[0]=="--acceptance-report").map(|p|PathBuf::from(&p[1]));
    set_windows_app_identity();
    let background=acceptance.is_some() || args.iter().any(|a|a=="--background");
    let mut builder=tauri::Builder::default();
    // A hidden acceptance instance has its own WebView profile and never raises the user's window.
    if acceptance.is_none(){builder=builder.plugin(tauri_plugin_single_instance::init(|app,args,_|{
        if !args.iter().any(|a|a=="--background"){show_main(app);}
    }));}
    builder.plugin(tauri_plugin_dialog::init()).setup(move|app|{
        let program=program_path().or_else(||app.dialog().file().set_title("选择已有的界渡工作区").blocking_pick_folder()
            .and_then(|p|p.into_path().ok()).map(|p|p.join(".jiedu/program")));
        let Some(program)=program else {app.handle().exit(0);return Ok(())};
        if !python(&program).is_file() || !program.join("knowledge_bridge/desktop_launcher.py").is_file(){
            app.dialog().message("未找到完整的界渡运行环境。请选择包含 .jiedu 的已有工作区。").title("界渡").kind(MessageDialogKind::Error).blocking_show();
            app.handle().exit(1);return Ok(())
        }
        // An update helper owns restart. A second shortcut click must not restart
        // the backend while files are being handed over to the installer.
        if let Some(request)=update_stop::pending(&program){
            let _=update_stop::acknowledge(&program,&request);app.handle().exit(0);return Ok(());
        }
        let version=load_version(&program);
        let initial_tooltip=tray_tooltip(version.product.as_deref(),"正在连接工作区");
        let menu=MenuBuilder::new(app).text("open","打开界渡").separator().text("reload","重新加载界面")
            .text("update","检查软件更新…").text("about","关于界渡").separator().text("stop","退出并停止后台").build()?;
        let tray_image=tauri::image::Image::new_owned(include_bytes!("../icons/tray.rgba").to_vec(),16,16);
        TrayIconBuilder::with_id("jiedu").icon(tray_image).menu(&menu)
            .tooltip(initial_tooltip).show_menu_on_left_click(false)
            .on_tray_icon_event(|tray,event|{
                if matches!(event,TrayIconEvent::Click{button:MouseButton::Left,button_state:MouseButtonState::Up,..}){show_main(tray.app_handle());}
            }).build(app)?;
        app.manage(Mutex::new(program.clone()));
        app.manage(Mutex::new(version));
        app.manage(Acceptance::default());
        let is_test=acceptance.is_some();
        let window=WebviewWindowBuilder::new(app,"main",WebviewUrl::App("index.html".into()))
            .title("界渡 · 私人知识工作区").inner_size(1320.,880.).min_inner_size(960.,640.)
            .theme(Some(tauri::Theme::Dark)).background_color(tauri::window::Color(17,27,32,255))
            .visible(!background).focused(!background)
            .initialization_script(if is_test { "document.addEventListener('DOMContentLoaded',()=>{if(location.hostname==='127.0.0.1' && document.querySelector('#brandSequence')) document.title='JIEDU_ACCEPTANCE_LOADED';});" } else { "" })
            .on_document_title_changed(move|w,title|{
                if is_test && title=="JIEDU_ACCEPTANCE_LOADED" {w.app_handle().state::<Acceptance>().loaded.store(true,Ordering::SeqCst);}
            })
            .data_directory(program.parent().unwrap().join(if is_test{"desktop-acceptance-data"}else{"desktop-data"})).build()?;
        window.set_icon(tauri::image::Image::new_owned(include_bytes!("../icons/window.rgba").to_vec(),32,32))?;
        app.state::<Acceptance>().window_icon_set.store(true,Ordering::SeqCst);
        let close_handle=app.handle().clone();
        window.on_window_event(move|event|{
            if let tauri::WindowEvent::CloseRequested{api,..}=event{
                api.prevent_close();
                close_handle.state::<Acceptance>().close_received.store(true,Ordering::SeqCst);
                if let Some(w)=close_handle.get_webview_window("main"){let _=w.hide();}
            }
        });
        if let Some(report)=acceptance {
            let qa=app.handle().clone();
            std::thread::spawn(move||{
                for _ in 0..120 {if qa.state::<Acceptance>().loaded.load(Ordering::SeqCst){break;}std::thread::sleep(std::time::Duration::from_millis(500));}
                if let Some(w)=qa.get_webview_window("main"){let _=w.close();}
                std::thread::sleep(std::time::Duration::from_secs(2));
                let window=qa.get_webview_window("main");
                refresh_version(&qa);
                let state=qa.state::<Mutex<VersionState>>();let state=state.lock().unwrap();
                let mut result=version_report(state.product.as_deref(),state.error.as_deref());drop(state);
                result.as_object_mut().unwrap().extend(serde_json::json!({"real_webview_loaded":qa.state::<Acceptance>().loaded.load(Ordering::SeqCst),
                    "close_event_received":qa.state::<Acceptance>().close_received.load(Ordering::SeqCst),
                    "window_survives_close":window.is_some(),"hidden_after_close":window.as_ref().map(|w|!w.is_visible().unwrap_or(true)).unwrap_or(false),
                    "tray_present":qa.tray_by_id("jiedu").is_some(),"no_focus_requested":true,"test_profile_isolated":true}).as_object().unwrap().clone());
                result["window_icon_set"]=serde_json::Value::Bool(qa.state::<Acceptance>().window_icon_set.load(Ordering::SeqCst));
                let current_tip=current_tooltip(&qa);
                result["tray_tooltip"]=serde_json::Value::String(current_tip);
                let _=std::fs::write(report,serde_json::to_vec_pretty(&result).unwrap());qa.exit(0);
            });
        }
        let handle=app.handle().clone();let window_id=window.label().to_string();
        let update_program=program.clone();let update_handle=app.handle().clone();
        std::thread::spawn(move||loop{
            std::thread::sleep(std::time::Duration::from_millis(250));
            if update_handle.get_webview_window("main").is_none(){break;}
            if exit_for_update(&update_handle,&update_program){break;}
        });
        std::thread::spawn(move||{
            if exit_for_update(&handle,&program){return;}
            match run_python(&program,"knowledge_bridge.desktop_launcher",&["start","--no-open"]){
                Ok(_)=>{
                    handle.state::<Acceptance>().connected.store(true,Ordering::SeqCst);
                    if let Some(t)=handle.tray_by_id("jiedu"){let _=t.set_tooltip(Some(current_tooltip(&handle)));}
                    if let Some(w)=handle.get_webview_window(&window_id){
                        // Background startup keeps a lightweight local page until the user opens the window.
                        if !background || is_test || w.is_visible().unwrap_or(false){let _=w.navigate(format!("http://127.0.0.1:{}/",port(&program)).parse().unwrap());}
                    }
                },
                Err(e)=>{if let Some(t)=handle.tray_by_id("jiedu"){let state=handle.state::<Mutex<VersionState>>();let state=state.lock().unwrap();let _=t.set_tooltip(Some(tray_tooltip(state.product.as_deref(),"启动未完成，请重新打开")));}if !background {handle.dialog().message(format!("启动未完成：{e}\n资料和进度仍保留，可重新打开界渡。")).title("界渡").kind(MessageDialogKind::Error).blocking_show();}}
            }
        });
        let version_handle=app.handle().clone();
        std::thread::spawn(move||loop{
            std::thread::sleep(std::time::Duration::from_secs(3));
            if version_handle.get_webview_window("main").is_none(){break;}
            refresh_version(&version_handle);
        });Ok(())
    }).on_menu_event(|app,event|{
        let program=app.state::<Mutex<PathBuf>>().lock().unwrap().clone();
        match event.id().as_ref(){
            "open"=>show_main(app),
            "reload"=>{if let Some(w)=app.get_webview_window("main"){let _=w.eval("location.reload()");}},
            "about"=>{refresh_version(app);let state=app.state::<Mutex<VersionState>>();let state=state.lock().unwrap();let message=about_message(state.product.as_deref());drop(state);app.dialog().message(message).title("关于界渡").show(|_|{});},
            "stop"=>{let handle=app.clone();std::thread::spawn(move||{
                match run_python(&program,"knowledge_bridge.desktop_launcher",&["stop","--no-open"]){
                    Ok(_)=>handle.exit(0),Err(e)=>{handle.dialog().message(e).title("停止未完成").blocking_show();}
                }
            });},
            "update"=>{
                if !exit_for_update(app,&program){
                    if let Some(w)=app.get_webview_window("main"){
                        let _=w.navigate(format!("http://127.0.0.1:{}/?open=software-update",port(&program)).parse().unwrap());
                        show_main(app);
                    }
                }
            },_=>{}
        }
    }).run(tauri::generate_context!()).expect("界渡窗口启动失败");
}

