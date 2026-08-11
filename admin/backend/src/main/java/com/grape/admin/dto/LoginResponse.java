package com.grape.admin.dto;

import lombok.Getter;
import lombok.Setter;

@Getter
@Setter
public class LoginResponse {

    private String token;
    private String refreshToken;
    private String username;
    private String nickname;
    private Long userId;
}
