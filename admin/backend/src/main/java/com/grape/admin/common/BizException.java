package com.grape.admin.common;

import org.springframework.http.HttpStatus;

/** 业务错误，默认 400；OBS 等基础设施故障可指定 502 */
public class BizException extends RuntimeException {
    private final HttpStatus status;

    public BizException(String message) {
        this(message, HttpStatus.BAD_REQUEST);
    }

    public BizException(String message, HttpStatus status) {
        super(message);
        this.status = status;
    }

    public HttpStatus getStatus() {
        return status;
    }
}
