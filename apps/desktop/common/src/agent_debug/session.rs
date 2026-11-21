//! Session（调试连接）：输入结束后完成已接受请求，按编号返回回执，再确认排空。
use super::{read_frame_into, write_frame, MAX_FRAME};
use serde_json::{json, Value};
use std::{collections::HashMap, future::Future, io, pin::Pin, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncWrite, BufReader},
    sync::{broadcast, mpsc, watch},
    task::{Id, JoinSet},
};

/// Dispatch（正式分发结果）：即时回执、异步处理或正常退出。
pub enum Dispatch {
    Reply(Result<Value, String>),
    Pending(Pin<Box<dyn Future<Output = Result<Value, String>> + Send>>),
    Stop(Result<Value, String>),
}

/// Limits（连接保护界限）：请求并发、请求完成与单帧写出等待上限。
pub struct Limits {
    /// requests（同时等待的异步请求上限）。
    pub requests: usize,
    /// request_timeout（单个调试请求等待上限，业务可能在其所属模块继续运行）。
    pub request_timeout: Duration,
    /// write_timeout（单帧回执写出等待上限）。
    pub write_timeout: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            requests: 16,
            request_timeout: Duration::from_secs(150),
            write_timeout: Duration::from_secs(10),
        }
    }
}

fn reply(id: Value, result: Result<Value, String>) -> Value {
    let value = match result {
        Ok(result) => json!({"jsonrpc":"2.0","id":id,"result":result}),
        Err(message) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":message}}),
    };
    if serde_json::to_vec(&value).is_ok_and(|bytes| bytes.len() < MAX_FRAME) {
        value
    } else {
        json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":"调试回执过长，请缩小查询范围或关闭截图。"}})
    }
}

async fn next_event(subscription: &mut Option<broadcast::Receiver<Value>>) -> Option<Value> {
    let receiver = subscription.as_mut()?;
    match receiver.recv().await {
        Ok(event) => Some(event),
        Err(broadcast::error::RecvError::Lagged(count)) => {
            Some(json!({"jsonrpc":"2.0","method":"trace.lagged","params":{"count":count}}))
        }
        Err(broadcast::error::RecvError::Closed) => None,
    }
}

/// serve（处理正式连接）：仅分发器决定权限和业务；这里统一处理传输生命周期。
/// 返回真表示已接受正常退出。授权撤销或真实断连取消仍在等待的调试请求。
pub async fn serve<S, H, F>(
    stream: S,
    mut revision: watch::Receiver<u64>,
    mut handle: H,
    mut release: F,
    limits: Limits,
) -> bool
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    H: FnMut(Value, &mut Option<broadcast::Receiver<Value>>) -> Dispatch,
    F: FnMut(),
{
    let (reader, mut writer) = tokio::io::split(stream);
    let mut reader = BufReader::new(reader);
    let mut bytes = Vec::new();
    let (output, mut messages) = mpsc::channel::<Value>(64);
    let mut writer_task = tokio::spawn(async move {
        while let Some(value) = messages.recv().await {
            tokio::time::timeout(limits.write_timeout, write_frame(&mut writer, &value))
                .await
                .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "调试回执写出超时。"))??;
        }
        Ok::<_, io::Error>(())
    });
    let mut subscription = None;
    let mut requests = JoinSet::new();
    let mut ids = HashMap::<Id, Value>::new();
    let mut finishing = false;
    let mut stopping = false;
    let mut connected = true;
    let mut writer_completed = false;
    loop {
        if finishing && requests.is_empty() {
            break;
        }
        tokio::select! {
            _ = &mut writer_task => { writer_completed = true; connected = false; break; }
            _ = revision.changed() => { connected = false; break; }
            done = requests.join_next_with_id(), if !requests.is_empty() => {
                let (task, outcome) = match done.unwrap() {
                    Ok((task, outcome)) => (task, outcome),
                    Err(error) => (error.id(), Err("调试请求执行异常，业务结果需重新查询。".into())),
                };
                if let Some(id) = ids.remove(&task) {
                    if output.send(reply(id, outcome)).await.is_err() { connected = false; break; }
                }
            }
            event = next_event(&mut subscription), if subscription.is_some() && !finishing => {
                if let Some(event) = event {
                    if output.send(event).await.is_err() { connected = false; break; }
                } else { subscription = None; }
            }
            frame = read_frame_into(&mut reader, &mut bytes) => {
                let Ok(Some(frame)) = frame else { connected = false; break; };
                // finish（输入已结束）之后不能追加请求；客户端仍保留读端接收回执。
                if finishing { connected = false; break; }
                let request: Value = match serde_json::from_slice(&frame) {
                    Ok(request) => request,
                    Err(_) => {
                        if output.send(reply(Value::Null, Err("调试消息不是有效的 JSON。".into()))).await.is_err() { connected = false; break; }
                        continue;
                    }
                };
                let id = request["id"].clone();
                let invalid = if request["jsonrpc"] != "2.0" {
                    Some("调试协议版本无效。")
                } else if request["method"] == "transport.finish" {
                    finishing = true;
                    release();
                    continue;
                } else if !id.is_string() && !id.is_i64() && !id.is_u64() {
                    Some("调试请求需要字符串或整数编号。")
                } else if ids.values().any(|pending| *pending == id) {
                    Some("调试请求编号重复，请为未完成请求使用不同编号。")
                } else { None };
                if let Some(error) = invalid {
                    if output.send(reply(id, Err(error.into()))).await.is_err() { connected = false; break; }
                    continue;
                }
                let result = match handle(request, &mut subscription) {
                    Dispatch::Pending(future) if requests.len() < limits.requests => {
                        let task = requests.spawn(async move {
                            tokio::time::timeout(limits.request_timeout, future).await
                                .unwrap_or_else(|_| Err("调试请求等待完成超时；业务可能仍在运行，请查询状态，避免重复提交。".into()))
                        });
                        ids.insert(task.id(), id);
                        continue;
                    }
                    Dispatch::Pending(_) => Err("调试并发请求已达上限，请等待已有回执后继续。".into()),
                    Dispatch::Reply(result) => result,
                    Dispatch::Stop(result) => {
                        stopping = result.is_ok();
                        finishing = stopping;
                        if stopping { release(); }
                        result
                    }
                };
                // 有界背压：等待写端腾出空间，不能因暂时队列满而丢弃已接受回执。
                if output.send(reply(id, result)).await.is_err() { connected = false; break; }
            }
        }
    }
    release();
    requests.abort_all();
    while requests.join_next().await.is_some() {}
    if connected && finishing {
        let _ = output.send(json!({"jsonrpc":"2.0","method":"transport.finished"})).await;
    }
    drop(output);
    if !connected && !writer_completed {
        writer_task.abort();
        let _ = writer_task.await;
    } else if connected && !writer_completed {
        // 单帧写出有独立时限；让队列真实排空，不按固定两秒截断大回执。
        let _ = writer_task.await;
    }
    if connected && finishing {
        // Windows（操作系统）命名管道关闭前，让客户端读完结束确认并关闭连接。
        let _ = tokio::time::timeout(Duration::from_secs(1), read_frame_into(&mut reader, &mut bytes)).await;
    }
    stopping
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent_debug::{read_frame, relay_stream};
    use tokio::io::AsyncWriteExt;

    async fn exchange<H>(lines: Vec<Vec<u8>>, handle: H, limits: Limits) -> Vec<Value>
    where
        H: FnMut(Value, &mut Option<broadcast::Receiver<Value>>) -> Dispatch + Send + 'static,
    {
        let (client, server) = tokio::io::duplex(128);
        let (_revision, changed) = watch::channel(0);
        let server = tokio::spawn(serve(server, changed, handle, || {}, limits));
        let (sender, receiver) = mpsc::channel(2);
        let input = tokio::spawn(async move {
            for line in lines { sender.send(Ok(line)).await.unwrap(); }
        });
        let mut output = Vec::new();
        tokio::time::timeout(Duration::from_secs(5), relay_stream(client, receiver, &mut output)).await.unwrap().unwrap();
        input.await.unwrap();
        server.await.unwrap();
        output.split(|byte| *byte == b'\n').filter(|line| !line.is_empty())
            .map(|line| serde_json::from_slice(line).unwrap()).collect()
    }

    fn request(id: Value, method: &str) -> Vec<u8> {
        serde_json::to_vec(&json!({"jsonrpc":"2.0","id":id,"method":method})).unwrap()
    }

    #[tokio::test]
    async fn agent_session_drains_single_async_request_after_input_eof() {
        let replies = exchange(vec![request(json!("snapshot"), "ui.snapshot")], |_, _| {
            Dispatch::Pending(Box::pin(async {
                tokio::time::sleep(Duration::from_millis(20)).await;
                Ok(json!({"controls":[]}))
            }))
        }, Limits::default()).await;
        assert_eq!(replies.len(), 1);
        assert_eq!(replies[0]["id"], "snapshot");
        assert!(replies[0]["result"]["controls"].is_array());
    }

    #[tokio::test]
    async fn agent_session_backpressure_keeps_every_reply_beyond_queue_capacity() {
        let lines = (0..2048).map(|id| request(json!(id), "instance.status")).collect();
        let replies = exchange(lines, |_, _| Dispatch::Reply(Ok(json!({"ready":true}))), Limits::default()).await;
        let ids: std::collections::HashSet<_> = replies.iter().map(|value| value["id"].as_u64().unwrap()).collect();
        assert_eq!(replies.len(), 2048);
        assert_eq!(ids.len(), 2048);
        assert!(replies.iter().all(|value| value["result"]["ready"] == true));
    }

    #[tokio::test]
    async fn agent_session_matches_out_of_order_results_by_string_and_integer_ids() {
        let replies = exchange(vec![request(json!("slow"), "slow"), request(json!(7), "fast")], |request, _| {
            Dispatch::Pending(Box::pin(async move {
                if request["method"] == "slow" { tokio::time::sleep(Duration::from_millis(20)).await; }
                Ok(request["method"].clone())
            }))
        }, Limits::default()).await;
        assert_eq!(replies.len(), 2);
        assert_eq!(replies[0]["id"], 7);
        assert_eq!(replies[1]["id"], "slow");
        assert_eq!(replies[1]["result"], "slow");
    }

    #[tokio::test]
    async fn agent_session_timeout_and_panic_both_return_numbered_errors() {
        let replies = exchange(vec![request(json!(1), "wait"), request(json!(2), "panic")], |request, _| {
            Dispatch::Pending(Box::pin(async move {
                if request["method"] == "panic" { panic!("测试处理器故障"); }
                std::future::pending().await
            }))
        }, Limits { request_timeout: Duration::from_millis(25), ..Limits::default() }).await;
        assert_eq!(replies.len(), 2);
        assert!(replies.iter().find(|value| value["id"] == 1).unwrap()["error"]["message"].as_str().unwrap().contains("超时"));
        assert!(replies.iter().find(|value| value["id"] == 2).unwrap()["error"]["message"].as_str().unwrap().contains("执行异常"));
    }

    #[tokio::test]
    async fn agent_session_invalid_input_errors_drain_without_losing_following_requests() {
        let replies = exchange(vec![b"invalid".to_vec(), b"{\"jsonrpc\":\"1.0\",\"id\":3}".to_vec(), request(json!(4), "unknown")], |_, _| {
            Dispatch::Reply(Err("未知调试方法。".into()))
        }, Limits::default()).await;
        assert_eq!(replies.len(), 3);
        assert!(replies[0]["id"].is_null());
        assert_eq!(replies[1]["id"], 3);
        assert_eq!(replies[2]["id"], 4);
        assert!(replies.iter().all(|value| value["error"].is_object()));
    }

    #[tokio::test]
    async fn agent_session_duplicate_and_concurrency_errors_preserve_original_request() {
        let replies = exchange(vec![request(json!(1), "slow"), request(json!(1), "status"), request(json!(2), "slow")], |_, _| {
            Dispatch::Pending(Box::pin(async {
                tokio::time::sleep(Duration::from_millis(20)).await;
                Ok(json!("original"))
            }))
        }, Limits { requests: 1, ..Limits::default() }).await;
        assert_eq!(replies.len(), 3);
        assert_eq!(replies.iter().filter(|value| value["result"] == "original").count(), 1);
        assert_eq!(replies.iter().filter(|value| value["error"].is_object()).count(), 2);
    }

    #[tokio::test]
    async fn agent_session_finish_releases_hook_before_waiting_for_request() {
        let released = std::sync::Arc::new(tokio::sync::Notify::new());
        let (mut client, server) = tokio::io::duplex(4096);
        let (_revision, changed) = watch::channel(0);
        let (waiting, release) = (released.clone(), released.clone());
        let server = tokio::spawn(serve(server, changed, move |_, _| {
            let waiting = waiting.clone();
            Dispatch::Pending(Box::pin(async move { waiting.notified().await; Ok(json!(true)) }))
        }, move || { release.notify_one(); }, Limits::default()));
        client.write_all(&request(json!(1), "hooked")).await.unwrap();
        client.write_all(b"\n{\"jsonrpc\":\"2.0\",\"method\":\"transport.finish\"}\n").await.unwrap();
        let mut reader = BufReader::new(client);
        let reply = tokio::time::timeout(Duration::from_secs(1), read_frame(&mut reader)).await.unwrap().unwrap().unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&reply).unwrap()["result"], true);
        assert!(read_frame(&mut reader).await.unwrap().is_some());
        drop(reader);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn agent_relay_rejects_disconnect_without_finished_even_after_a_reply() {
        let (client, mut server) = tokio::io::duplex(4096);
        let (sender, receiver) = mpsc::channel(1);
        sender.send(Ok(request(json!(1), "status"))).await.unwrap();
        drop(sender);
        tokio::spawn(async move {
            server.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":true}\n").await.unwrap();
        });
        let error = relay_stream(client, receiver, Vec::new()).await.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::UnexpectedEof);
    }

    #[tokio::test]
    async fn agent_session_revocation_cancels_pending_without_success_confirmation() {
        let (client, server) = tokio::io::duplex(4096);
        let (revision, changed) = watch::channel(0);
        let (accepted, started) = tokio::sync::oneshot::channel();
        let mut accepted = Some(accepted);
        let server = tokio::spawn(serve(server, changed, move |_, _| {
            let _ = accepted.take().unwrap().send(());
            Dispatch::Pending(Box::pin(std::future::pending()))
        }, || {}, Limits::default()));
        let (sender, receiver) = mpsc::channel(1);
        sender.send(Ok(request(json!(1), "wait"))).await.unwrap();
        let relay = tokio::spawn(relay_stream(client, receiver, Vec::new()));
        started.await.unwrap();
        revision.send(1).unwrap();
        assert!(relay.await.unwrap().is_err());
        server.await.unwrap();
    }

    #[tokio::test]
    async fn agent_session_keeps_subscription_and_following_requests_on_one_connection() {
        let (mut client, server) = tokio::io::duplex(4096);
        let (_revision, changed) = watch::channel(0);
        let (events, _) = broadcast::channel(8);
        let subscription = events.clone();
        let server = tokio::spawn(serve(server, changed, move |request, receiver| {
            if request["method"] == "trace.subscribe" { *receiver = Some(subscription.subscribe()); }
            Dispatch::Reply(Ok(json!(true)))
        }, || {}, Limits::default()));
        client.write_all(&request(json!(1), "trace.subscribe")).await.unwrap();
        client.write_all(b"\n").await.unwrap();
        let mut reader = BufReader::new(client);
        assert!(read_frame(&mut reader).await.unwrap().is_some());
        events.send(json!({"jsonrpc":"2.0","method":"trace.event","params":{"point":"test"}})).unwrap();
        let event: Value = serde_json::from_slice(&read_frame(&mut reader).await.unwrap().unwrap()).unwrap();
        assert_eq!(event["method"], "trace.event");
        reader.get_mut().write_all(&request(json!(2), "instance.status")).await.unwrap();
        reader.get_mut().write_all(b"\n{\"jsonrpc\":\"2.0\",\"method\":\"transport.finish\"}\n").await.unwrap();
        let reply: Value = serde_json::from_slice(&read_frame(&mut reader).await.unwrap().unwrap()).unwrap();
        assert_eq!(reply["id"], 2);
        let finish: Value = serde_json::from_slice(&read_frame(&mut reader).await.unwrap().unwrap()).unwrap();
        assert_eq!(finish["method"], "transport.finished");
        drop(reader);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn agent_session_rejects_oversized_reply_without_dropping_connection() {
        let replies = exchange(vec![request(json!(1), "large")], |_, _| {
            Dispatch::Reply(Ok(json!("x".repeat(MAX_FRAME))))
        }, Limits::default()).await;
        assert_eq!(replies.len(), 1);
        assert_eq!(replies[0]["id"], 1);
        assert!(replies[0]["error"]["message"].as_str().unwrap().contains("回执过长"));
    }

    #[tokio::test]
    async fn agent_session_stalled_reader_releases_connection_after_write_timeout() {
        let (mut client, server) = tokio::io::duplex(128);
        let (_revision, changed) = watch::channel(0);
        let server = tokio::spawn(serve(server, changed, |_, _| {
            Dispatch::Reply(Ok(json!("x".repeat(4096))))
        }, || {}, Limits { write_timeout: Duration::from_millis(25), ..Limits::default() }));
        client.write_all(&request(json!(1), "large")).await.unwrap();
        client.write_all(b"\n").await.unwrap();
        tokio::time::timeout(Duration::from_secs(1), server).await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn agent_relay_can_finish_when_host_accepts_exit_with_input_still_open() {
        let (client, server) = tokio::io::duplex(4096);
        let (_revision, changed) = watch::channel(0);
        let server = tokio::spawn(serve(server, changed, |_, _| {
            Dispatch::Stop(Ok(json!({"accepted":true})))
        }, || {}, Limits::default()));
        let (sender, receiver) = mpsc::channel(1);
        sender.send(Ok(request(json!(1), "instance.exit"))).await.unwrap();
        let mut output = Vec::new();
        tokio::time::timeout(Duration::from_secs(1), relay_stream(client, receiver, &mut output)).await.unwrap().unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&output).unwrap()["result"]["accepted"], true);
        assert!(server.await.unwrap());
        drop(sender);
    }
}
