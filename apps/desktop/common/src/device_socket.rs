//! DeviceSocket（设备同步套接字）：保留同步协议接口，等待就绪、执行超时并响应服务停止。
use std::{
    io::{self, Read, Write},
    net::TcpStream,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

const CANCEL_INTERVAL: Duration = Duration::from_millis(100); // CANCEL_INTERVAL（等待时重新检查停止标志的最长间隔）。

pub struct CancellableSocket {
    stream: TcpStream,       // stream（显式配置为非阻塞的已接受连接）。
    stop: Arc<AtomicBool>,   // stop（所属服务的停止标志）。
    read_timeout: Duration,  // read_timeout（每次读取允许的无进展时限）。
    write_timeout: Duration, // write_timeout（每次写入允许的无进展时限）。
}

impl CancellableSocket {
    /// new（准备同步等待）：WouldBlock（暂不可读写）交给系统就绪等待处理。
    pub fn new(
        stream: TcpStream,
        stop: Arc<AtomicBool>,
        read_timeout: Duration,
        write_timeout: Duration,
    ) -> io::Result<Self> {
        stream.set_nonblocking(true)?;
        stream.set_nodelay(true)?;
        Ok(Self {
            stream,
            stop,
            read_timeout,
            write_timeout,
        })
    }

    /// try_clone（克隆回写句柄）：读写共享同一服务停止标志。
    pub fn try_clone(&self) -> io::Result<Self> {
        Ok(Self {
            stream: self.stream.try_clone()?,
            stop: self.stop.clone(),
            read_timeout: self.read_timeout,
            write_timeout: self.write_timeout,
        })
    }

    fn check_stop(&self) -> io::Result<()> {
        if self.stop.load(Ordering::Acquire) {
            Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "设备通道服务正在停止。",
            ))
        } else {
            Ok(())
        }
    }

    fn wait(&self, writing: bool, started: Instant) -> io::Result<()> {
        self.check_stop()?;
        let timeout = if writing {
            self.write_timeout
        } else {
            self.read_timeout
        };
        let remaining = timeout
            .checked_sub(started.elapsed())
            .filter(|value| !value.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "设备通道等待数据超时。"))?;
        let interval = remaining.min(CANCEL_INTERVAL);
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawSocket;
            use windows_sys::Win32::Networking::WinSock::{
                WSAGetLastError, WSAPoll, POLLRDNORM, POLLWRNORM, SOCKET_ERROR, WSAPOLLFD,
            };
            let mut descriptor = WSAPOLLFD {
                fd: self.stream.as_raw_socket() as usize,
                events: if writing { POLLWRNORM } else { POLLRDNORM },
                revents: 0,
            };
            // WSAPoll（Windows 套接字就绪等待）只借用当前持有的句柄，不转移或关闭其所有权。
            if unsafe { WSAPoll(&mut descriptor, 1, interval.as_millis().max(1) as i32) }
                == SOCKET_ERROR
            {
                let error = io::Error::from_raw_os_error(unsafe { WSAGetLastError() });
                if error.kind() != io::ErrorKind::Interrupted {
                    return Err(error);
                }
            }
        }
        #[cfg(not(windows))]
        {
            let _ = writing;
            std::thread::sleep(interval.min(Duration::from_millis(1)));
        }
        self.check_stop()
    }
}

impl Read for CancellableSocket {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let started = Instant::now();
        loop {
            self.check_stop()?;
            match self.stream.read(bytes) {
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    self.wait(false, started)?
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                result => return result,
            }
        }
    }
}

impl Write for CancellableSocket {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let started = Instant::now();
        loop {
            self.check_stop()?;
            match self.stream.write(bytes) {
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    self.wait(true, started)?
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                result => return result,
            }
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        self.check_stop()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{net::TcpListener, sync::mpsc, thread};

    fn pair(timeout: Duration) -> (CancellableSocket, TcpStream, Arc<AtomicBool>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (stream, _) = listener.accept().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        (
            CancellableSocket::new(stream, stop.clone(), timeout, timeout).unwrap(),
            client,
            stop,
        )
    }

    #[test]
    fn read_timeout_does_not_poison_the_next_read() {
        let (mut stream, mut client, _) = pair(Duration::from_millis(120));
        assert_eq!(
            stream.read(&mut [0_u8]).unwrap_err().kind(),
            io::ErrorKind::TimedOut
        );
        client.write_all(b"ok").unwrap();
        let mut bytes = [0_u8; 2];
        stream.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, b"ok");
    }

    #[test]
    fn stop_cancels_idle_read_without_waiting_for_the_peer_to_close() {
        let (mut stream, _client, stop) = pair(Duration::from_secs(60));
        let (tx, rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            tx.send(stream.read(&mut [0_u8]).unwrap_err().kind())
                .unwrap()
        });
        thread::sleep(Duration::from_millis(100));
        stop.store(true, Ordering::Release);
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(2)).unwrap(),
            io::ErrorKind::ConnectionAborted
        );
        worker.join().unwrap();
    }

    #[test]
    fn stop_cancels_backpressured_writes_without_peer_reads() {
        let (mut stream, _client, stop) = pair(Duration::from_secs(60));
        let (tx, rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            let bytes = [0_u8; 64 * 1024];
            loop {
                if let Err(error) = stream.write_all(&bytes) {
                    tx.send(error.kind()).unwrap();
                    break;
                }
            }
        });
        thread::sleep(Duration::from_millis(100));
        stop.store(true, Ordering::Release);
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(2)).unwrap(),
            io::ErrorKind::ConnectionAborted
        );
        worker.join().unwrap();
    }
}
