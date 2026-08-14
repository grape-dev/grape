package com.grape.admin.dto;

import lombok.Getter;
import lombok.Setter;

import java.util.List;

@Getter
@Setter
public class RoleDto {

    private Long id;
    private String code;
    private String name;
    private Integer isProtected;
    private List<Long> menuIds;
}
