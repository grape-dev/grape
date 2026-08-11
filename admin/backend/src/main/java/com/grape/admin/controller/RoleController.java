package com.grape.admin.controller;

import com.grape.admin.dto.CreateRoleRequest;
import com.grape.admin.dto.RoleDto;
import com.grape.admin.service.RoleService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import javax.servlet.http.HttpServletRequest;
import javax.validation.Valid;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/admin/roles")
public class RoleController {

    private final RoleService roleService;

    public RoleController(RoleService roleService) {
        this.roleService = roleService;
    }

    @GetMapping
    public List<RoleDto> list() {
        return roleService.listRoles();
    }

    @PostMapping
    public RoleDto create(@Valid @RequestBody CreateRoleRequest req, HttpServletRequest request) {
        return wrap(() -> roleService.createRole(req, request));
    }

    @DeleteMapping("/{id}")
    public Map<String, String> delete(@PathVariable Long id, HttpServletRequest request) {
        wrap(() -> {
            roleService.deleteRole(id, request);
            return null;
        });
        return Map.of("status", "ok");
    }

    @GetMapping("/{id}/permissions")
    public Map<String, List<Long>> permissions(@PathVariable Long id) {
        return Map.of("menuIds", roleService.getPermissions(id));
    }

    @PutMapping("/{id}/permissions")
    public Map<String, String> savePermissions(@PathVariable Long id,
                                               @RequestBody Map<String, List<Long>> body,
                                               HttpServletRequest request) {
        wrap(() -> {
            roleService.savePermissions(id, body.get("menuIds"), request);
            return null;
        });
        return Map.of("status", "ok");
    }

    private <T> T wrap(java.util.function.Supplier<T> supplier) {
        try {
            return supplier.get();
        } catch (RoleService.BizException e) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, e.getMessage());
        }
    }
}
