package com.grape.admin.controller;

import com.grape.admin.dto.LoginRequest;
import com.grape.admin.dto.LoginResponse;
import com.grape.admin.service.AuthService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import javax.validation.Valid;
import java.util.Map;

@RestController
@RequestMapping("/api/admin/auth")
public class AuthController {

    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    @PostMapping("/login")
    public LoginResponse login(@Valid @RequestBody LoginRequest req) {
        try {
            return authService.login(req.getUsername(), req.getPassword());
        } catch (AuthService.AuthException e) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, e.getMessage());
        }
    }

    @PostMapping("/refresh")
    public LoginResponse refresh(@RequestBody Map<String, String> body) {
        try {
            return authService.refresh(body.get("refreshToken"));
        } catch (AuthService.AuthException e) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, e.getMessage());
        }
    }

    @PostMapping("/logout")
    public Map<String, String> logout(@RequestBody Map<String, String> body) {
        authService.logout(body.get("refreshToken"));
        return Map.of("status", "ok");
    }
}
