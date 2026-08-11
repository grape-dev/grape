package com.grape.admin.controller;

import com.grape.admin.dto.AssignRolesRequest;
import com.grape.admin.dto.CreateUserRequest;
import com.grape.admin.dto.UserDto;
import com.grape.admin.service.UserService;
import org.springframework.data.domain.Page;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import javax.servlet.http.HttpServletRequest;
import javax.validation.Valid;
import java.util.Map;

@RestController
@RequestMapping("/api/admin/users")
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    @GetMapping
    public Page<UserDto> list(@RequestParam(defaultValue = "") String keyword,
                              @RequestParam(defaultValue = "0") int page,
                              @RequestParam(defaultValue = "20") int size) {
        return userService.listUsers(keyword, page, Math.min(size, 100));
    }

    @PostMapping
    public UserDto create(@Valid @RequestBody CreateUserRequest req, HttpServletRequest request) {
        return wrap(() -> userService.createUser(req, request));
    }

    @PutMapping("/{id}/status")
    public Map<String, String> updateStatus(@PathVariable Long id,
                                            @RequestBody Map<String, Integer> body,
                                            HttpServletRequest request) {
        wrap(() -> {
            userService.updateStatus(id, body.get("status"), request);
            return null;
        });
        return Map.of("status", "ok");
    }

    @PutMapping("/{id}/roles")
    public Map<String, String> assignRoles(@PathVariable Long id,
                                           @RequestBody AssignRolesRequest req,
                                           HttpServletRequest request) {
        wrap(() -> {
            userService.assignRoles(id, req.getRoleIds(), request);
            return null;
        });
        return Map.of("status", "ok");
    }

    private <T> T wrap(java.util.function.Supplier<T> supplier) {
        try {
            return supplier.get();
        } catch (UserService.BizException e) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, e.getMessage());
        }
    }
}
